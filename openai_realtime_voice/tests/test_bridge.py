"""
Tests for the Realtime Voice Bridge.

Happy-flow test runs the real code path: client connects to bridge,
bridge calls websockets.connect() to upstream. Catches handler signature
and client API (e.g. additional_headers) bugs.

Audio pacing tests verify that the token-bucket sender never exceeds
the 48 000 B/s playback rate, preventing queue overflow on the ESP32.
"""
import asyncio
import json
import os
import sys
import time
import base64

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


@pytest.mark.asyncio
async def test_client_connect_bridge_connects_to_upstream():
    """
    Happy flow: client connects to bridge; bridge handle_client runs and
    calls _connect_openai() (real websockets.connect). Fails if handler
    signature is wrong or connect() gets bad kwargs (e.g. extra_headers).
    """
    _ensure_env()

    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    fake_server_task = asyncio.create_task(_fake_openai_server(FAKE_OPENAI_PORT))
    bridge_task = asyncio.create_task(bridge.run())

    await asyncio.sleep(0.5)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            await client_ws.send(b"\x00\x00")
    except Exception as e:
        pytest.fail(f"Bridge should accept client connection: {e}")
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass
        try:
            await fake_server_task
        except asyncio.CancelledError:
            pass
        _cleanup_env()


@pytest.mark.asyncio
async def test_connect_openai_uses_additional_headers():
    """Regression: _connect_openai must use additional_headers (websockets 13+), not extra_headers."""
    fake_port = 19996
    os.environ["OPENAI_API_KEY"] = "test"
    os.environ["OPENAI_REALTIME_URL"] = f"ws://127.0.0.1:{fake_port}"
    os.environ["WEBSOCKET_PORT"] = "19997"
    os.environ["WEBSOCKET_HOST"] = "127.0.0.1"

    from app.main import RealtimeVoiceBridge
    import app.main as main_module

    bridge = RealtimeVoiceBridge()
    fake_server_task = asyncio.create_task(_fake_openai_server(fake_port))
    await asyncio.sleep(0.2)

    connect_calls = []
    original_connect = main_module.websockets.connect

    async def record_connect(*args, **kwargs):
        connect_calls.append(kwargs)
        ws = await original_connect(*args, **kwargs)
        return ws

    try:
        with pytest.MonkeyPatch.context() as m:
            m.setattr(main_module.websockets, "connect", record_connect)
            openai_ws = await bridge._connect_openai()
            await openai_ws.close()
        assert len(connect_calls) == 1
        assert "additional_headers" in connect_calls[0]
        assert "extra_headers" not in connect_calls[0]
        assert "Authorization" in connect_calls[0]["additional_headers"]
        assert "OpenAI-Beta" in connect_calls[0]["additional_headers"]
    finally:
        fake_server_task.cancel()
        try:
            await fake_server_task
        except asyncio.CancelledError:
            pass
        _cleanup_env()


# ---------------------------------------------------------------------------
# Regression: handle_client finally block must not use .closed (websockets 13+)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_client_finally_no_attribute_error():
    """
    When the client disconnects, handle_client's finally block closes the
    OpenAI ws. It must not access .closed (AttributeError in websockets 13+).
    """
    _ensure_env()

    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    fake_server_task = asyncio.create_task(_fake_openai_server(FAKE_OPENAI_PORT))
    bridge_task = asyncio.create_task(bridge.run())

    await asyncio.sleep(0.5)

    handler_errors = []
    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            await client_ws.send(b"\x00\x00")
            await asyncio.sleep(0.2)
        # client_ws is now closed; bridge's handle_client finally block runs
        await asyncio.sleep(0.5)
    except Exception as e:
        handler_errors.append(e)
    finally:
        bridge_task.cancel()
        fake_server_task.cancel()
        try:
            await bridge_task
        except asyncio.CancelledError:
            pass
        try:
            await fake_server_task
        except asyncio.CancelledError:
            pass
        _cleanup_env()

    assert not handler_errors, f"handle_client raised: {handler_errors}"


# ---------------------------------------------------------------------------
# Audio pacing: _audio_sender must not exceed 48 000 B/s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_sender_paces_at_realtime_rate():
    """
    Feed 2 seconds of audio (96 000 bytes) into the queue all at once.
    Verify that _audio_sender takes ~2 s to deliver it (not <1 s).
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

    audio_queue: asyncio.Queue = asyncio.Queue()
    done = asyncio.Event()
    fake_ws = FakeClientWs()

    total_bytes = BYTES_PER_SEC * 2  # 2 seconds of audio
    chunk_size = 4800  # ~100 ms per delta (realistic OpenAI chunk)
    audio_data = b"\x00" * total_bytes

    # Enqueue all audio at once (simulates OpenAI burst)
    for offset in range(0, total_bytes, chunk_size):
        await audio_queue.put(audio_data[offset : offset + chunk_size])
    await audio_queue.put(None)  # sentinel: flush

    sender_task = asyncio.create_task(
        bridge._audio_sender(fake_ws, audio_queue, done)
    )

    # Wait for sender to finish (should take ~2 s); timeout at 4 s
    await asyncio.sleep(3.5)
    done.set()
    try:
        await asyncio.wait_for(sender_task, timeout=1.0)
    except asyncio.TimeoutError:
        sender_task.cancel()

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
async def test_audio_sender_flush_on_sentinel():
    """Partial buffer (< 960 bytes) is flushed when sentinel (None) arrives."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()

    sent_chunks: list[bytes] = []

    class FakeClientWs:
        async def send(self, data):
            sent_chunks.append(data)

    audio_queue: asyncio.Queue = asyncio.Queue()
    done = asyncio.Event()

    # Put a small chunk (less than SEND_CHUNK_SIZE) then sentinel
    small_chunk = b"\x00" * 500
    await audio_queue.put(small_chunk)
    await audio_queue.put(None)

    sender_task = asyncio.create_task(
        bridge._audio_sender(FakeClientWs(), audio_queue, done)
    )
    await asyncio.sleep(0.5)
    done.set()
    try:
        await asyncio.wait_for(sender_task, timeout=1.0)
    except asyncio.TimeoutError:
        sender_task.cancel()

    total_sent = sum(len(c) for c in sent_chunks)
    assert total_sent == 500, f"Expected 500 bytes flushed, got {total_sent}"

    _cleanup_env()


@pytest.mark.asyncio
async def test_clear_audio_queue_on_interrupt():
    """_clear_audio_queue drops all pending chunks."""
    _ensure_env()
    from app.main import RealtimeVoiceBridge

    audio_queue: asyncio.Queue = asyncio.Queue()
    for _ in range(20):
        await audio_queue.put(b"\x00" * 960)

    assert audio_queue.qsize() == 20
    RealtimeVoiceBridge._clear_audio_queue(audio_queue)
    assert audio_queue.empty()

    _cleanup_env()
