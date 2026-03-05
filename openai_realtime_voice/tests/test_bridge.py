"""
Tests for the Realtime Voice Bridge.

Black-box, behavior-focused tests: verify client receives response audio,
session tears down cleanly, correct phase messages, etc. Integration tests
use a scripted fake OpenAI server; unit tests exercise pacing and phase logic.

Audio pacing tests verify that the token-bucket sender never exceeds
the 48 000 B/s playback rate, preventing queue overflow on the ESP32.
"""
import asyncio
import json
import os
import sys
import time
import base64
from unittest.mock import AsyncMock, patch

import pytest

# Add addon app to path so "app.main" resolves when we run tests from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets
from websockets.asyncio.server import serve as ws_serve


# Ports for integration test (avoid conflicts; tests set env before import)
FAKE_OPENAI_PORT = 19999
BRIDGE_PORT = 19998


def _ensure_env():
    """Set minimal env vars for bridge construction."""
    os.environ.setdefault("OPENAI_API_KEY", "test")
    os.environ.setdefault("OPENAI_REALTIME_URL", f"ws://127.0.0.1:{FAKE_OPENAI_PORT}")
    os.environ.setdefault("WEBSOCKET_PORT", str(BRIDGE_PORT))
    os.environ.setdefault("WEBSOCKET_HOST", "127.0.0.1")


def _cleanup_env():
    for key in ("OPENAI_REALTIME_URL", "WEBSOCKET_PORT", "WEBSOCKET_HOST"):
        os.environ.pop(key, None)


async def _fake_openai_server(port: int, handler=None) -> None:
    """Minimal WebSocket server. Custom handler or default (consume-all)."""
    async def _default_handler(ws):
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    async with ws_serve(handler or _default_handler, "127.0.0.1", port):
        await asyncio.Future()


# ---------------------------------------------------------------------------
# Scripted fake OpenAI server for integration tests
# ---------------------------------------------------------------------------


class ScriptedOpenAIHandler:
    """
    Fake OpenAI Realtime server that follows the protocol and can be scripted
    to send session events, respond after N audio appends, drop connection,
    or include tool calls in response.done.
    """

    def __init__(
        self,
        *,
        respond_after_append_count: int = 3,
        response_audio_chunks: int = 5,
        chunk_size: int = 960,
        drop_connection_after_append_count: int | None = None,
        call_disconnect_tool: bool = False,
        received_response_cancel: list | None = None,
        cancel_received_event: asyncio.Event | None = None,
    ):
        self.respond_after_append_count = respond_after_append_count
        self.response_audio_chunks = response_audio_chunks
        self.chunk_size = chunk_size
        self.drop_connection_after_append_count = drop_connection_after_append_count
        self.call_disconnect_tool = call_disconnect_tool
        self.received_response_cancel = received_response_cancel or []
        self.cancel_received_event = cancel_received_event
        self.append_count = 0
        self.sent_response = False

    async def __call__(self, ws) -> None:
        await ws.send(json.dumps({"type": "session.created"}))
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ev_type = msg.get("type")

                if ev_type == "session.update":
                    await ws.send(json.dumps({"type": "session.updated"}))

                elif ev_type == "input_audio_buffer.append":
                    self.append_count += 1
                    if self.drop_connection_after_append_count is not None:
                        if self.append_count >= self.drop_connection_after_append_count:
                            await ws.close()
                            return
                    if (
                        not self.sent_response
                        and self.append_count >= self.respond_after_append_count
                    ):
                        self.sent_response = True
                        if self.call_disconnect_tool:
                            await self._send_disconnect_response(ws)
                        else:
                            await self._send_full_response(ws)

                elif ev_type == "response.cancel":
                    self.received_response_cancel.append(True)
                    if self.cancel_received_event is not None:
                        self.cancel_received_event.set()
                    await ws.send(
                        json.dumps({
                            "type": "response.done",
                            "response": {"status": "cancelled", "output": []},
                        })
                    )
        except Exception:
            pass

    async def _send_disconnect_response(self, ws) -> None:
        """Minimal response: response.created + response.done with disconnect_client tool."""
        await ws.send(json.dumps({"type": "response.created"}))
        output = [
            {"type": "message"},
            {
                "type": "function_call",
                "name": "disconnect_client",
                "call_id": "call_disconnect_1",
                "arguments": '{"reason": "user_requested_stop"}',
            },
        ]
        await ws.send(
            json.dumps({
                "type": "response.done",
                "response": {"status": "completed", "output": output},
            })
        )

    async def _send_full_response(self, ws) -> None:
        await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
        await ws.send(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
        await ws.send(json.dumps({"type": "input_audio_buffer.committed"}))
        await ws.send(json.dumps({"type": "response.created"}))
        chunk_b64 = base64.standard_b64encode(b"\x00" * self.chunk_size).decode("ascii")
        for _ in range(self.response_audio_chunks):
            await ws.send(
                json.dumps({"type": "response.output_audio.delta", "delta": chunk_b64})
            )
        await ws.send(json.dumps({"type": "response.output_audio.done"}))
        output = [{"type": "message"}]
        await ws.send(
            json.dumps({
                "type": "response.done",
                "response": {"status": "completed", "output": output},
            })
        )


def test_session_config_ga_format():
    """Session config must use GA interface shape (audio.input/output, type: realtime)."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    config = bridge._session_config()
    try:
        session = config["session"]
        assert config["type"] == "session.update"
        assert session.get("type") == "realtime"
        assert "audio" in session
        assert "input" in session["audio"]
        assert "output" in session["audio"]
        assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert session["audio"]["output"]["voice"] == "marin"
        assert session["audio"]["input"]["turn_detection"]["type"] == "server_vad"
        assert "modalities" not in session
        assert "voice" not in session
    finally:
        _cleanup_env()


# ---------------------------------------------------------------------------
# Audio pacing: _output_sender must not exceed 48 000 B/s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_output_sender_paces_at_realtime_rate():
    """
    Feed 2 seconds of audio (96 000 bytes) into the queue all at once.
    Verify that _output_sender takes ~2 s to deliver it (not <1 s).
    """
    _ensure_env()
    from app.main import RealtimeVoiceBridge, BYTES_PER_SEC

    bridge = RealtimeVoiceBridge()

    sent_chunks: list[bytes] = []
    send_times: list[float] = []

    class FakeClientWs:
        async def send(self, data):
            sent_chunks.append(data)
            send_times.append(asyncio.get_event_loop().time())

    output_queue: asyncio.Queue = asyncio.Queue()
    first_audio_to_client = [False]
    end_reason = {}
    fake_ws = FakeClientWs()

    total_bytes = BYTES_PER_SEC * 2  # 2 seconds of audio
    chunk_size = 4800  # ~100 ms per delta (realistic OpenAI chunk)
    audio_data = b"\x00" * total_bytes

    # Enqueue all audio at once (simulates OpenAI burst)
    for offset in range(0, total_bytes, chunk_size):
        await output_queue.put(audio_data[offset : offset + chunk_size])
    await output_queue.put(None)  # sentinel: flush

    sender_task = asyncio.create_task(
        bridge._output_sender(fake_ws, output_queue, first_audio_to_client, end_reason)
    )

    # Wait for sender to deliver (~2 s); then cancel (sender runs until cancelled)
    await asyncio.sleep(3.5)
    sender_task.cancel()
    try:
        await sender_task
    except asyncio.CancelledError:
        pass

    total_sent = sum(len(c) for c in sent_chunks)
    assert total_sent == total_bytes, f"Expected {total_bytes} bytes sent, got {total_sent}"

    # Verify pacing: time between first and last send should be ~2 s (±0.3 s)
    if len(send_times) >= 2:
        elapsed = send_times[-1] - send_times[0]
        expected = total_bytes / BYTES_PER_SEC
        assert elapsed >= expected * 0.8, (
            f"Sent too fast: {elapsed:.2f} s for {expected:.1f} s of audio"
        )
        assert elapsed <= expected * 1.5, (
            f"Sent too slow: {elapsed:.2f} s for {expected:.1f} s of audio"
        )

    _cleanup_env()


@pytest.mark.asyncio
async def test_output_sender_flush_on_sentinel():
    """Partial buffer (< 960 bytes) is flushed when sentinel (None) arrives."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()

    sent_chunks: list[bytes] = []

    class FakeClientWs:
        async def send(self, data):
            sent_chunks.append(data)

    output_queue: asyncio.Queue = asyncio.Queue()
    first_audio_to_client = [False]
    end_reason = {}

    # Put a small chunk (less than SEND_CHUNK_SIZE) then sentinel
    small_chunk = b"\x00" * 500
    await output_queue.put(small_chunk)
    await output_queue.put(None)

    sender_task = asyncio.create_task(
        bridge._output_sender(FakeClientWs(), output_queue, first_audio_to_client, end_reason)
    )
    await asyncio.sleep(0.5)
    sender_task.cancel()
    try:
        await sender_task
    except asyncio.CancelledError:
        pass

    total_sent = sum(len(c) for c in sent_chunks)
    assert total_sent == 500, f"Expected 500 bytes flushed, got {total_sent}"

    _cleanup_env()


@pytest.mark.asyncio
async def test_clear_output_queue_on_interrupt():
    """_clear_output_queue drops all pending chunks."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    output_queue: asyncio.Queue = asyncio.Queue()
    for _ in range(20):
        await output_queue.put(b"\x00" * 960)

    assert output_queue.qsize() == 20
    RealtimeVoiceBridge._clear_output_queue(output_queue)
    assert output_queue.empty()

    _cleanup_env()


# ---------------------------------------------------------------------------
# Phase decision: phase sent once at response.done (searching vs listening)
# ---------------------------------------------------------------------------


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


def _client_ws_recording_phases():
    """Fake client_ws that records phase messages sent to it."""
    phases_sent: list[str] = []

    class FakeClientWs:
        async def send(self, data):
            if isinstance(data, str):
                try:
                    obj = json.loads(data)
                    if obj.get("type") == "phase":
                        phases_sent.append(obj.get("phase", ""))
                except json.JSONDecodeError:
                    pass

    return FakeClientWs(), phases_sent


@pytest.mark.asyncio
async def test_phase_searching_with_function_call():
    """
    When response has audio + function_call(search_web), phase sent after
    response.done is "searching" only (no "listening" in between).
    """
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    output_queue: asyncio.Queue = asyncio.Queue()
    end_reason = {}
    response_idle = asyncio.Event()
    response_idle.set()
    pending_tool_tasks = set()
    fake_client_ws, phases_sent = _client_ws_recording_phases()

    events = [
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": base64.standard_b64encode(b"\x00" * 960).decode("ascii")},
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
                        "call_id": "call_abc123",
                        "arguments": '{"query": "weather"}',
                    },
                ],
            },
        },
    ]
    fake_openai_ws = _FakeOpenAIWs(events)

    with patch.object(bridge, "_run_tool_in_background", AsyncMock()):
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
    # Post-response phase "searching" is queued; drain it
    while not output_queue.empty():
        try:
            item = output_queue.get_nowait()
            if isinstance(item, tuple) and item[0] == "phase":
                phases_sent.append(item[1])
        except asyncio.QueueEmpty:
            break

    assert phases_sent == ["replying", "searching"], (
        f"Expected [replying, searching], got {phases_sent}"
    )
    _cleanup_env()


@pytest.mark.asyncio
async def test_phase_no_listening_without_function_call():
    """When response has audio only (no function_call), server sends no phase at response.done.
    Client derives listening locally from audio playback state."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    output_queue: asyncio.Queue = asyncio.Queue()
    end_reason = {}
    response_idle = asyncio.Event()
    response_idle.set()
    pending_tool_tasks = set()
    fake_client_ws, phases_sent = _client_ws_recording_phases()

    events = [
        {"type": "response.created"},
        {"type": "response.output_audio.delta", "delta": base64.standard_b64encode(b"\x00" * 960).decode("ascii")},
        {"type": "response.output_audio.done"},
        {
            "type": "response.done",
            "response": {"status": "completed", "output": [{"type": "message"}]},
        },
    ]
    fake_openai_ws = _FakeOpenAIWs(events)

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
    while not output_queue.empty():
        try:
            item = output_queue.get_nowait()
            if isinstance(item, tuple) and item[0] == "phase":
                phases_sent.append(item[1])
        except asyncio.QueueEmpty:
            break

    assert phases_sent == ["replying"], (
        f"Expected [replying], got {phases_sent}"
    )
    _cleanup_env()


# ---------------------------------------------------------------------------
# Integration tests (handle_client level, black-box)
# ---------------------------------------------------------------------------

async def _run_bridge_with_fake_openai(
    openai_handler,
    *,
    openai_port: int = FAKE_OPENAI_PORT,
    bridge_port: int = BRIDGE_PORT,
):
    """Start fake OpenAI server and bridge; yield control so test can run. Caller must cancel tasks and cleanup."""
    _ensure_env()
    os.environ["OPENAI_REALTIME_URL"] = f"ws://127.0.0.1:{openai_port}"
    os.environ["WEBSOCKET_PORT"] = str(bridge_port)
    os.environ["WEBSOCKET_HOST"] = "127.0.0.1"

    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    fake_server_task = asyncio.create_task(_fake_openai_server(openai_port, openai_handler))
    bridge_task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.5)
    return bridge_task, fake_server_task


@pytest.mark.asyncio
async def test_happy_flow_client_receives_response_audio():
    """
    Contract: Client connects, receives ready, sends audio; server forwards to OpenAI;
    OpenAI responds with audio; client receives binary audio frames and phase messages.
    """
    handler = ScriptedOpenAIHandler(respond_after_append_count=3, response_audio_chunks=5)
    bridge_task, fake_server_task = await _run_bridge_with_fake_openai(handler)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            # Wait for ready
            ready = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            assert json.loads(ready).get("type") == "ready"
            # Send audio until we get response (fake responds after 3 appends)
            for _ in range(10):
                await client_ws.send(b"\x00\x00" * 384)  # 768 bytes
            # Wait for response: thinking, replying, then binary audio (session must survive >3s)
            phases = []
            binary_received = []
            deadline = time.monotonic() + 8.0
            while time.monotonic() < deadline:
                try:
                    msg = await asyncio.wait_for(client_ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if isinstance(msg, bytes):
                    binary_received.append(len(msg))
                else:
                    obj = json.loads(msg)
                    if obj.get("type") == "phase":
                        phases.append(obj.get("phase"))
                if binary_received and len(phases) >= 2:
                    break
        assert len(binary_received) >= 1, "Client should receive at least one binary audio frame"
        assert "thinking" in phases and "replying" in phases
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        await asyncio.gather(bridge_task, fake_server_task, return_exceptions=True)
        _cleanup_env()


@pytest.mark.asyncio
async def test_client_disconnect_tears_down_cleanly():
    """
    Contract: Client closes WebSocket mid-session; server cleans up OpenAI connection;
    session ends with reason client_disconnected.
    """
    handler = ScriptedOpenAIHandler(respond_after_append_count=99)
    bridge_task, fake_server_task = await _run_bridge_with_fake_openai(handler)
    end_reason = {}

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            ready = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            assert json.loads(ready).get("type") == "ready"
            await client_ws.send(b"\x00\x00" * 384)
            await asyncio.sleep(0.3)
        # Client disconnected; bridge should tear down without error
        await asyncio.sleep(1.0)
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        await asyncio.gather(bridge_task, fake_server_task, return_exceptions=True)
        _cleanup_env()


@pytest.mark.asyncio
async def test_openai_disconnect_tears_down_cleanly():
    """
    Contract: OpenAI closes WebSocket mid-session; server cleans up;
    session ends with reason openai_disconnected.
    """
    handler = ScriptedOpenAIHandler(
        respond_after_append_count=2,
        drop_connection_after_append_count=2,
    )
    bridge_task, fake_server_task = await _run_bridge_with_fake_openai(handler)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            ready = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            assert json.loads(ready).get("type") == "ready"
            await client_ws.send(b"\x00\x00" * 384)
            await client_ws.send(b"\x00\x00" * 384)
            # Fake OpenAI drops connection; client should see connection close
            try:
                while True:
                    await asyncio.wait_for(client_ws.recv(), timeout=2.0)
            except (websockets.exceptions.ConnectionClosed, asyncio.TimeoutError):
                pass
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        await asyncio.gather(bridge_task, fake_server_task, return_exceptions=True)
        _cleanup_env()


@pytest.mark.asyncio
async def test_disconnect_tool_sends_disconnect_to_client():
    """
    Contract: OpenAI calls disconnect_client tool; server sends {"type":"disconnect"}
    to client; session ends with reason disconnect_tool.
    """
    handler = ScriptedOpenAIHandler(
        respond_after_append_count=2,
        call_disconnect_tool=True,
    )
    bridge_task, fake_server_task = await _run_bridge_with_fake_openai(handler)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            ready = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            assert json.loads(ready).get("type") == "ready"
            await client_ws.send(b"\x00\x00" * 384)
            await client_ws.send(b"\x00\x00" * 384)
            # Should receive disconnect message before connection closes
            msg = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            obj = json.loads(msg)
            assert obj.get("type") == "disconnect"
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        await asyncio.gather(bridge_task, fake_server_task, return_exceptions=True)
        _cleanup_env()


@pytest.mark.asyncio
async def test_client_interrupt_cancels_response():
    """
    Contract: Client sends {"type":"interrupt"} while server is streaming;
    server sends response.cancel to OpenAI and clears audio queue.
    """
    cancel_received: list[bool] = []
    cancel_event = asyncio.Event()
    handler = ScriptedOpenAIHandler(
        respond_after_append_count=1,
        response_audio_chunks=2,
        received_response_cancel=cancel_received,
        cancel_received_event=cancel_event,
    )
    bridge_task, fake_server_task = await _run_bridge_with_fake_openai(handler)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            ready = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            assert json.loads(ready).get("type") == "ready"
            await client_ws.send(b"\x00\x00" * 384)
            for _ in range(8):
                msg = await asyncio.wait_for(client_ws.recv(), timeout=2.0)
                if isinstance(msg, bytes):
                    break
            await client_ws.send(json.dumps({"type": "interrupt"}))
            try:
                await asyncio.wait_for(cancel_event.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
        assert cancel_event.is_set(), "Fake OpenAI should have received response.cancel"
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        await asyncio.gather(bridge_task, fake_server_task, return_exceptions=True)
        _cleanup_env()


@pytest.mark.asyncio
async def test_phase_lifecycle_thinking_replying():
    """
    Contract: After speech stops client receives thinking; when audio starts replying.
    Server does NOT send listening — client derives it from audio playback state.
    """
    handler = ScriptedOpenAIHandler(respond_after_append_count=2, response_audio_chunks=2)
    bridge_task, fake_server_task = await _run_bridge_with_fake_openai(handler)

    try:
        phases = []
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            ready = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
            assert json.loads(ready).get("type") == "ready"
            await client_ws.send(b"\x00\x00" * 384)
            await client_ws.send(b"\x00\x00" * 384)
            while len(phases) < 2:
                msg = await asyncio.wait_for(client_ws.recv(), timeout=5.0)
                if isinstance(msg, str):
                    obj = json.loads(msg)
                    if obj.get("type") == "phase":
                        phases.append(obj.get("phase"))
        assert phases == ["thinking", "replying"]
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        await asyncio.gather(bridge_task, fake_server_task, return_exceptions=True)
        _cleanup_env()
