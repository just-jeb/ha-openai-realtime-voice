"""
Tests for the Realtime Voice Bridge.

Happy-flow test runs the real code path: client connects to bridge,
bridge calls websockets.connect() to upstream. Catches handler signature
and client API (e.g. additional_headers) bugs.
"""
import asyncio
import os
import sys

import pytest

# Add addon app to path so "app.main" resolves when we run tests from repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import websockets
from websockets.asyncio.server import serve as ws_serve


# Ports for integration test (avoid conflicts; tests set env before import)
FAKE_OPENAI_PORT = 19999
BRIDGE_PORT = 19998


async def _fake_openai_server(port: int) -> None:
    """Minimal WebSocket server that accepts one connection and echoes until closed."""
    async def handler(ws):
        try:
            async for _ in ws:
                pass
        except Exception:
            pass

    async with ws_serve(handler, "127.0.0.1", port):
        await asyncio.Future()


@pytest.mark.asyncio
async def test_client_connect_bridge_connects_to_upstream():
    """
    Happy flow: client connects to bridge; bridge handle_client runs and
    calls _connect_openai() (real websockets.connect). Fails if handler
    signature is wrong or connect() gets bad kwargs (e.g. extra_headers).
    """
    os.environ["OPENAI_API_KEY"] = "test"
    os.environ["OPENAI_REALTIME_URL"] = f"ws://127.0.0.1:{FAKE_OPENAI_PORT}"
    os.environ["WEBSOCKET_PORT"] = str(BRIDGE_PORT)
    os.environ["WEBSOCKET_HOST"] = "127.0.0.1"

    from app.main import RealtimeVoiceBridge

    bridge = RealtimeVoiceBridge()
    fake_server_task = asyncio.create_task(_fake_openai_server(FAKE_OPENAI_PORT))
    bridge_task = asyncio.create_task(bridge.run())

    # Let server and bridge start
    await asyncio.sleep(0.5)

    try:
        async with websockets.connect(f"ws://127.0.0.1:{BRIDGE_PORT}/") as client_ws:
            # Connection accepted = handle_client was called with one arg and didn't crash
            # Bridge will have called _connect_openai() with additional_headers
            await client_ws.send(b"\x00\x00")  # minimal binary frame (PCM)
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
        # Clean env so other tests don't inherit
        for key in ("OPENAI_REALTIME_URL", "WEBSOCKET_PORT", "WEBSOCKET_HOST"):
            os.environ.pop(key, None)


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
        for key in ("OPENAI_REALTIME_URL", "WEBSOCKET_PORT", "WEBSOCKET_HOST"):
            os.environ.pop(key, None)
