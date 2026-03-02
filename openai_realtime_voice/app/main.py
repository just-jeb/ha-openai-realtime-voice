"""
Direct WebSocket bridge: ESP32 Voice PE <-> OpenAI Realtime API.

One client connection = one OpenAI Realtime session. Lifecycle is correct by
construction (try/finally closes upstream on disconnect).
"""
import os
import sys
import asyncio
import json
import base64
import logging
import uuid
from typing import Optional

import dotenv
import websockets
import httpx

from app.audio_recorder import AudioRecorder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)
logging.getLogger("websockets").setLevel(logging.WARNING)

dotenv.load_dotenv()

# Contract: 24kHz, 16-bit, mono PCM (both directions)
SAMPLE_RATE = 24000

TOOLS = [
    {
        "type": "function",
        "name": "disconnect_client",
        "description": "End the conversation when the user says goodbye, farewell, stop, or wants to finish. Use when the user explicitly wants to end the conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "enum": ["user_requested_stop", "conversation_ended"],
                }
            },
            "required": ["reason"],
        },
    },
    {
        "type": "function",
        "name": "search_web",
        "description": "Search the web for current/live information. Use for questions about today's events, weather, news, schedules, or anything that requires up-to-date information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"}
            },
            "required": ["query"],
        },
    },
]


class RealtimeVoiceBridge:
    """Bridges ESP32 WebSocket to OpenAI Realtime API."""

    def __init__(self) -> None:
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        self.web_search_api_key = os.environ.get("WEB_SEARCH_API_KEY", "") or self.openai_api_key
        self.websocket_host = os.environ.get("WEBSOCKET_HOST", "0.0.0.0")
        self.websocket_port = int(os.environ.get("WEBSOCKET_PORT", "8080"))
        self.instructions = os.environ.get(
            "INSTRUCTIONS",
            "You are a friendly voice assistant. Keep answers clear and concise.",
        )
        self.vad_threshold = float(os.environ.get("VAD_THRESHOLD", "0.5"))
        self.vad_prefix_padding_ms = int(os.environ.get("VAD_PREFIX_PADDING_MS", "300"))
        self.vad_silence_duration_ms = int(os.environ.get("VAD_SILENCE_DURATION_MS", "500"))
        self.enable_recording = os.environ.get("ENABLE_RECORDING", "false").lower() == "true"
        self.recorder: Optional[AudioRecorder] = None
        if self.enable_recording:
            self.recorder = AudioRecorder(output_dir="recordings")

        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY environment variable is required")

    def _session_config(self) -> dict:
        """Build session.update payload for OpenAI Realtime."""
        return {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": self.instructions,
                "voice": "marin",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": self.vad_threshold,
                    "prefix_padding_ms": self.vad_prefix_padding_ms,
                    "silence_duration_ms": self.vad_silence_duration_ms,
                },
                "tools": TOOLS,
            },
        }

    async def _connect_openai(self):
        """Open WebSocket to OpenAI Realtime API."""
        url = os.environ.get(
            "OPENAI_REALTIME_URL", "wss://api.openai.com/v1/realtime?model=gpt-realtime"
        )
        additional_headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "OpenAI-Beta": "realtime=v1",
        }
        ws = await websockets.connect(
            url,
            additional_headers=additional_headers,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        logger.info("Connected to OpenAI Realtime API")
        return ws

    async def _configure_session(self, openai_ws) -> None:
        """Send session.update so the model is ready for audio."""
        event = self._session_config()
        await openai_ws.send(json.dumps(event))
        logger.debug("Sent session.update")

    def _client_id_from_ws(self, ws) -> str:
        """Derive a client id (e.g. for recording)."""
        if hasattr(ws, "remote_address") and ws.remote_address:
            return ws.remote_address[0].replace(".", "_")
        return f"client_{uuid.uuid4().hex[:8]}"

    async def _client_to_openai(
        self,
        client_ws,
        openai_ws,
        done: asyncio.Event,
        client_id: str,
    ) -> None:
        """Forward ESP32 binary audio and JSON control to OpenAI."""
        try:
            async for message in client_ws:
                if done.is_set():
                    break
                if isinstance(message, bytes):
                    # Raw PCM -> base64 -> input_audio_buffer.append
                    if len(message) % 2 != 0:
                        message = message + b"\x00"
                    b64 = base64.standard_b64encode(message).decode("ascii")
                    event = {"type": "input_audio_buffer.append", "audio": b64}
                    await openai_ws.send(json.dumps(event))
                    if self.recorder:
                        self.recorder.record_input_audio(message)
                else:
                    # Text: interrupt or other JSON
                    try:
                        data = json.loads(message) if isinstance(message, str) else message
                        if data.get("type") == "interrupt":
                            logger.info("Interrupt received from client")
                            await openai_ws.send(json.dumps({"type": "response.cancel"}))
                    except (json.JSONDecodeError, TypeError):
                        pass
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"Client connection closed: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"Error in client_to_openai: {e}", exc_info=True)
        finally:
            done.set()

    async def _openai_to_client(
        self,
        client_ws,
        openai_ws,
        done: asyncio.Event,
        client_id: str,
    ) -> None:
        """Forward OpenAI events to ESP32; handle audio and tool calls."""
        try:
            async for raw in openai_ws:
                if done.is_set():
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ev_type = event.get("type")

                if ev_type == "response.audio.delta":
                    delta_b64 = event.get("delta")
                    if delta_b64:
                        audio_bytes = base64.standard_b64decode(delta_b64)
                        await client_ws.send(audio_bytes)
                        if self.recorder:
                            self.recorder.record_output_audio(audio_bytes)

                elif ev_type == "response.done":
                    response = event.get("response", {})
                    output = response.get("output", [])
                    for item in output:
                        if item.get("type") == "function_call":
                            await self._handle_tool_call(
                                client_ws, openai_ws, item, done
                            )

                elif ev_type == "error":
                    logger.error(f"OpenAI error: {event.get('message', event)}")
                elif ev_type == "session.created":
                    logger.debug("Session created")
                elif ev_type == "session.updated":
                    logger.debug("Session updated")
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"OpenAI connection closed: {e.code} {e.reason}")
        except Exception as e:
            logger.error(f"Error in openai_to_client: {e}", exc_info=True)
        finally:
            done.set()

    async def _handle_tool_call(
        self,
        client_ws,
        openai_ws,
        item: dict,
        done: asyncio.Event,
    ) -> None:
        """Execute tool and send result back to OpenAI."""
        name = item.get("name")
        call_id = item.get("call_id")
        args_str = item.get("arguments", "{}")
        if not call_id:
            return
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}

        if name == "disconnect_client":
            logger.info("Disconnect tool called")
            result = json.dumps({"success": True, "message": "Disconnected"})
            await self._send_tool_result(openai_ws, call_id, result)
            try:
                await client_ws.send(
                    json.dumps({
                        "type": "disconnect",
                        "message": "User requested disconnect",
                        "reason": args.get("reason", "user_requested_stop"),
                    })
                )
                await asyncio.sleep(0.1)
            except Exception:
                pass
            try:
                await client_ws.close()
            except Exception:
                pass
            done.set()
            return

        if name == "search_web":
            query = args.get("query", "")
            if not query:
                await self._send_tool_result(
                    openai_ws, call_id, json.dumps({"error": "Missing query"})
                )
                return
            try:
                text = await self._web_search(query)
                await self._send_tool_result(openai_ws, call_id, json.dumps({"result": text}))
            except Exception as e:
                logger.warning(f"Web search failed: {e}")
                await self._send_tool_result(
                    openai_ws, call_id, json.dumps({"error": str(e)})
                )
            return

        await self._send_tool_result(
            openai_ws, call_id, json.dumps({"error": f"Unknown tool: {name}"})
        )

    async def _send_tool_result(self, openai_ws, call_id: str, output: str) -> None:
        """Send function_call_output item and trigger response.create."""
        await openai_ws.send(
            json.dumps({
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            })
        )
        await openai_ws.send(json.dumps({"type": "response.create"}))

    async def _web_search(self, query: str) -> str:
        """Call OpenAI Responses API with web search."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.web_search_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-4.1-mini",
                    "input": query,
                    "tools": [{"type": "web_search_preview"}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        # Extract text from first output item of type message
        output = data.get("output", [])
        for item in output:
            if item.get("type") == "message":
                content = item.get("content", [])
                for part in content:
                    if part.get("type") == "output_text":
                        return part.get("text", "")
        return "No result from web search."

    async def handle_client(self, client_ws) -> None:
        """One client connection = one OpenAI session. Always."""
        client_id = self._client_id_from_ws(client_ws)
        logger.info(f"Client connected: {client_id}")

        if self.recorder:
            self.recorder.start_recording(client_id)

        openai_ws = None
        try:
            openai_ws = await self._connect_openai()
            await self._configure_session(openai_ws)
            done = asyncio.Event()
            await asyncio.gather(
                self._client_to_openai(client_ws, openai_ws, done, client_id),
                self._openai_to_client(client_ws, openai_ws, done, client_id),
            )
        except Exception as e:
            logger.error(f"Session error: {e}", exc_info=True)
        finally:
            if openai_ws and not openai_ws.closed:
                await openai_ws.close()
            if self.recorder:
                self.recorder.stop_recording()
            logger.info(f"Client session ended: {client_id}")

    async def run(self) -> None:
        """Start WebSocket server and run forever."""
        logger.info(
            f"Starting WebSocket server on ws://{self.websocket_host}:{self.websocket_port}/"
        )
        async with websockets.serve(
            self.handle_client,
            self.websocket_host,
            self.websocket_port,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        ):
            await asyncio.Future()


async def main() -> None:
    bridge = RealtimeVoiceBridge()
    try:
        await bridge.run()
    except KeyboardInterrupt:
        logger.info("Shutting down")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
