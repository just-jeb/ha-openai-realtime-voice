"""
Tests for server-side phase decisions in _openai_to_client.

Verifies that the server sends only thinking/replying/searching phases
(never "listening") and that the had_audio variable has been removed.
Uses a mock OpenAI websocket yielding scripted events and a real asyncio.Queue.
"""
import asyncio
import base64
import inspect
import json
import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

AUDIO_B64 = base64.standard_b64encode(b"\x00" * 960).decode("ascii")


def _ensure_env():
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("OPENAI_REALTIME_URL", "ws://127.0.0.1:19999")
    os.environ.setdefault("WEBSOCKET_PORT", "19998")
    os.environ.setdefault("WEBSOCKET_HOST", "127.0.0.1")


def _cleanup_env():
    for key in ("OPENAI_REALTIME_URL", "WEBSOCKET_PORT", "WEBSOCKET_HOST"):
        os.environ.pop(key, None)


class _FakeOpenAIWs:
    """Async iterator yielding scripted OpenAI events (JSON strings)."""

    def __init__(self, events: list[dict]):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return json.dumps(self._events.pop(0))


def _drain_phases(output_queue: asyncio.Queue) -> list[str]:
    """Drain all phase tuples from the output queue."""
    phases = []
    while not output_queue.empty():
        try:
            item = output_queue.get_nowait()
            if isinstance(item, tuple) and item[0] == "phase":
                phases.append(item[1])
        except asyncio.QueueEmpty:
            break
    return phases


async def _run_openai_to_client(events, pending_tool_tasks=None, mock_tool=None):
    """Run _openai_to_client with scripted events and return queued phases."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    output_queue: asyncio.Queue = asyncio.Queue()
    end_reason = {}
    response_idle = asyncio.Event()
    response_idle.set()
    if pending_tool_tasks is None:
        pending_tool_tasks = set()

    fake_client_ws = AsyncMock()
    fake_openai_ws = _FakeOpenAIWs(events)

    mock_fn = mock_tool or AsyncMock()
    with patch.object(bridge, "_run_tool_in_background", mock_fn):
        await bridge._openai_to_client(
            fake_client_ws,
            fake_openai_ws,
            "test",
            output_queue,
            end_reason,
            response_idle,
            pending_tool_tasks,
            {},
        )

    phases = _drain_phases(output_queue)
    _cleanup_env()
    return phases


# ---------------------------------------------------------------------------
# Test cases matching the scenario table
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_chat():
    """Normal chat: speech_stop -> audio -> audio.done -> response.done(message).
    Expected phases: [thinking, replying]. No listening from server."""
    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": AUDIO_B64},
        {"type": "response.output_audio.done"},
        {
            "type": "response.done",
            "response": {"status": "completed", "output": [{"type": "message"}]},
        },
    ]
    phases = await _run_openai_to_client(events)
    assert phases == ["thinking", "replying"]
    assert "listening" not in phases


@pytest.mark.asyncio
async def test_search_only():
    """Search only: speech_stop -> response.done(function_call).
    Expected phases: [thinking, searching]."""
    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "search_web",
                        "call_id": "call_1",
                        "arguments": '{"query": "weather"}',
                    }
                ],
            },
        },
    ]
    phases = await _run_openai_to_client(events)
    assert phases == ["thinking", "searching"]
    assert "listening" not in phases


@pytest.mark.asyncio
async def test_audio_and_tool_same_response():
    """Audio + tool in same response: speech_stop -> audio -> audio.done -> response.done(message + function_call).
    Expected phases: [thinking, replying, searching]."""
    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": AUDIO_B64},
        {"type": "response.output_audio.done"},
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "message"},
                    {
                        "type": "function_call",
                        "name": "search_web",
                        "call_id": "call_1",
                        "arguments": '{"query": "weather"}',
                    },
                ],
            },
        },
    ]
    phases = await _run_openai_to_client(events)
    assert phases == ["thinking", "replying", "searching"]
    assert "listening" not in phases


@pytest.mark.asyncio
async def test_two_responses_audio_then_tool():
    """Two responses: audio then tool. speech_stop -> audio -> audio.done -> response.done(message)
    -> response.created -> response.done(function_call).
    Expected phases: [thinking, replying, searching]."""
    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": AUDIO_B64},
        {"type": "response.output_audio.done"},
        {
            "type": "response.done",
            "response": {"status": "completed", "output": [{"type": "message"}]},
        },
        {"type": "response.created"},
        {
            "type": "response.done",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "search_web",
                        "call_id": "call_2",
                        "arguments": '{"query": "weather"}',
                    }
                ],
            },
        },
    ]
    phases = await _run_openai_to_client(events)
    assert phases == ["thinking", "replying", "searching"]
    assert "listening" not in phases


@pytest.mark.asyncio
async def test_user_speaks_during_search():
    """User speaks while a search is pending. pending_tool_tasks is non-empty (pre-populated).
    speech_stop -> audio -> audio.done -> response.done(message, pending_tools>0).
    Expected phases: [thinking, replying, searching]."""
    # Pre-populate pending_tool_tasks with a never-completing task
    pending_tool_tasks: set = set()
    never_done = asyncio.Event()

    async def mock_pending():
        await never_done.wait()

    mock_task = asyncio.create_task(mock_pending())
    pending_tool_tasks.add(mock_task)

    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": AUDIO_B64},
        {"type": "response.output_audio.done"},
        {
            "type": "response.done",
            "response": {"status": "completed", "output": [{"type": "message"}]},
        },
    ]
    try:
        phases = await _run_openai_to_client(events, pending_tool_tasks=pending_tool_tasks)
        assert phases == ["thinking", "replying", "searching"]
        assert "listening" not in phases
    finally:
        mock_task.cancel()
        try:
            await mock_task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_empty_response():
    """Empty response: speech_stop -> response.done(no output).
    Expected phases: [thinking] only."""
    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {
            "type": "response.done",
            "response": {"status": "completed", "output": []},
        },
    ]
    phases = await _run_openai_to_client(events)
    assert phases == ["thinking"]
    assert "listening" not in phases


@pytest.mark.asyncio
async def test_error_response():
    """Error response: speech_stop -> response.done(status=failed).
    Expected phases: [thinking] only."""
    events = [
        {"type": "input_audio_buffer.speech_stopped"},
        {"type": "response.created"},
        {
            "type": "response.done",
            "response": {
                "status": "failed",
                "status_details": "internal_error",
                "output": [],
            },
        },
    ]
    phases = await _run_openai_to_client(events)
    assert phases == ["thinking"]
    assert "listening" not in phases


# ---------------------------------------------------------------------------
# Source code verification: had_audio must be removed
# ---------------------------------------------------------------------------


def test_had_audio_removed():
    """Verify had_audio variable was removed from _openai_to_client."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    source = inspect.getsource(RealtimeVoiceBridge._openai_to_client)
    _cleanup_env()
    assert "had_audio" not in source, "had_audio variable should be removed from _openai_to_client"
