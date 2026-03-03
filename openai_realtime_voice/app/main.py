"""
Direct WebSocket bridge: ESP32 Voice PE <-> OpenAI Realtime API.

One client connection = one OpenAI Realtime session. When any forwarding task
ends (client disconnect, OpenAI disconnect, or disconnect tool), the others
are cancelled and both WebSockets are closed immediately.

Audio pacing: OpenAI streams response audio faster than real-time. The ESP32
client consumes at 24 kHz × 2 bytes = 48 000 bytes/sec. A token-bucket paced
sender buffers incoming deltas and sends to the client at playback rate.
"""
import os
import sys
import asyncio
import json
import base64
import logging
import time
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

# #region agent log
_DBG_PATH = "/Users/jeb/projects/ha-openai-realtime-voice/.cursor/debug-47ae00.log"
def _dbg(hyp, loc, msg, data=None):
    entry = json.dumps({"sessionId":"47ae00","hypothesisId":hyp,"location":loc,"message":msg,"data":data or {},"timestamp":int(time.time()*1000)})
    logger.info("[DBG-47ae00][%s] %s: %s %r", hyp, loc, msg, data or {})
    try:
        with open(_DBG_PATH, "a") as f:
            f.write(entry + "\n")
    except Exception:
        pass
# #endregion

# Contract: 24kHz, 16-bit, mono PCM (both directions)
SAMPLE_RATE = 24000
BYTES_PER_SEC = SAMPLE_RATE * 2  # 16-bit = 2 bytes per sample → 48 000 B/s
SEND_CHUNK_SIZE = BYTES_PER_SEC * 20 // 1000  # 960 bytes = 20 ms of audio

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
        self.web_search_api_key = (os.environ.get("WEB_SEARCH_API_KEY") or "").strip() or self.openai_api_key
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
        """Build session.update payload for OpenAI Realtime (GA interface)."""
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "instructions": self.instructions,
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": self.vad_threshold,
                            "prefix_padding_ms": self.vad_prefix_padding_ms,
                            "silence_duration_ms": self.vad_silence_duration_ms,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                        "voice": "marin",
                    },
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

    async def _wait_for_event(self, openai_ws, expected_type: str, timeout: float = 5.0):
        """Read OpenAI events until the expected type arrives. Returns the event."""
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for {expected_type}")
            raw = await asyncio.wait_for(openai_ws.recv(), timeout=remaining)
            if isinstance(raw, str):
                event = json.loads(raw)
                ev_type = event.get("type")
                logger.info("OpenAI setup event: %s", ev_type)
                if ev_type == "error":
                    logger.error("OpenAI error during setup: %s", event)
                    raise RuntimeError(
                        f"OpenAI error: {event.get('error', {}).get('message', event)}"
                    )
                if ev_type == expected_type:
                    return event

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
        client_id: str,
        audio_queue: asyncio.Queue,
        first_audio_from_client: list,
        end_reason: dict,
    ) -> None:
        """Forward ESP32 binary audio and JSON control to OpenAI. Client only sends audio
        after receiving ready, so openai_ws is always valid here."""
        # #region agent log
        _dbg("H1", "c2o:entry", "task_started", {"client_id": client_id})
        # #endregion
        msg_count = 0
        try:
            async for message in client_ws:
                # #region agent log
                msg_count += 1
                if msg_count <= 3:
                    _dbg("H1", "c2o:msg", "received", {"n": msg_count, "type": "bin" if isinstance(message, bytes) else "txt", "len": len(message)})
                # #endregion
                if isinstance(message, bytes):
                    if not first_audio_from_client[0]:
                        first_audio_from_client[0] = True
                        logger.info("First audio from client (%d bytes)", len(message))
                    if len(message) % 2 != 0:
                        message = message + b"\x00"
                    b64 = base64.standard_b64encode(message).decode("ascii")
                    await openai_ws.send(
                        json.dumps({"type": "input_audio_buffer.append", "audio": b64})
                    )
                    if self.recorder:
                        self.recorder.record_input_audio(message)
                else:
                    try:
                        data = json.loads(message) if isinstance(message, str) else message
                        if data.get("type") == "interrupt":
                            logger.info("Interrupt received from client")
                            self._clear_audio_queue(audio_queue)
                            await openai_ws.send(json.dumps({"type": "response.cancel"}))
                    except (json.JSONDecodeError, TypeError):
                        pass
        except websockets.exceptions.ConnectionClosed as e:
            # #region agent log
            _dbg("H1", "c2o:closed", "connection_closed", {"code": e.code, "msgs_received": msg_count})
            # #endregion
            end_reason["reason"] = "client_disconnected"
            end_reason["code"] = e.code
            logger.info("client_to_openai ended (reason: connection_closed, code: %s)", e.code)
        except asyncio.CancelledError:
            # #region agent log
            _dbg("H3", "c2o:cancelled", "task_cancelled", {"msgs_received": msg_count})
            # #endregion
            raise
        except Exception as e:
            # #region agent log
            _dbg("H1", "c2o:error", "unexpected_error", {"error": str(e), "msgs_received": msg_count})
            # #endregion
            logger.error("Error in client_to_openai: %s", e, exc_info=True)
            end_reason["reason"] = "client_to_openai_error"
        finally:
            if "reason" not in end_reason:
                end_reason["reason"] = "client_disconnected"

    async def _send_phase(self, client_ws, phase: str) -> None:
        """Send phase message to client for LED/UX feedback."""
        try:
            await client_ws.send(json.dumps({"type": "phase", "phase": phase}))
        except Exception:
            pass

    async def _openai_to_client(
        self,
        client_ws,
        openai_ws,
        client_id: str,
        audio_queue: asyncio.Queue,
        end_reason: dict,
        response_idle: asyncio.Event,
        pending_tool_tasks: set,
    ) -> None:
        """Forward OpenAI events to ESP32; audio goes through paced queue. Sends phase
        messages (thinking/replying/listening) for client LED feedback."""
        # #region agent log
        _dbg("H3", "o2c:entry", "task_started", {"client_id": client_id})
        # #endregion
        delta_count = 0
        delta_bytes_total = 0
        sent_replying_phase = False
        try:
            async for raw in openai_ws:
                if not isinstance(raw, str):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ev_type = event.get("type")

                if ev_type == "response.output_audio.delta":
                    delta_b64 = event.get("delta")
                    if delta_b64:
                        if not sent_replying_phase:
                            sent_replying_phase = True
                            await self._send_phase(client_ws, "replying")
                        audio_bytes = base64.standard_b64decode(delta_b64)
                        await audio_queue.put(audio_bytes)
                        delta_count += 1
                        delta_bytes_total += len(audio_bytes)
                        if self.recorder:
                            self.recorder.record_output_audio(audio_bytes)

                elif ev_type == "response.output_audio.done":
                    await audio_queue.put(None)
                    sent_replying_phase = False
                    await self._send_phase(client_ws, "listening")
                    duration_s = delta_bytes_total / BYTES_PER_SEC if delta_bytes_total else 0
                    logger.info(
                        "Response audio complete: %d deltas, %d bytes (%.1f s)",
                        delta_count, delta_bytes_total, duration_s,
                    )
                    delta_count = 0
                    delta_bytes_total = 0

                elif ev_type == "response.created":
                    response_idle.clear()
                    logger.info("OpenAI new response started")

                elif ev_type == "response.done":
                    response = event.get("response", {})
                    response_idle.set()
                    status = response.get("status")
                    if status and status != "completed":
                        logger.warning(
                            "Response status=%s details=%s",
                            status,
                            response.get("status_details"),
                        )
                    output = response.get("output", [])
                    for item in output:
                        if item.get("type") == "function_call":
                            if item.get("name") == "disconnect_client":
                                should_stop = await self._handle_tool_call(
                                    client_ws, openai_ws, item, audio_queue
                                )
                                if should_stop:
                                    end_reason["reason"] = "disconnect_tool"
                                    return
                            else:
                                task = asyncio.create_task(
                                    self._run_tool_in_background(
                                        openai_ws, item, response_idle
                                    )
                                )
                                pending_tool_tasks.add(task)
                                task.add_done_callback(pending_tool_tasks.discard)

                elif ev_type == "error":
                    logger.error("OpenAI error: %s", event.get("message", event))
                elif ev_type == "session.created":
                    logger.debug("Session created")
                elif ev_type == "session.updated":
                    logger.debug("Session updated")
                elif ev_type == "input_audio_buffer.speech_started":
                    logger.info("OpenAI detected speech start")
                elif ev_type == "input_audio_buffer.speech_stopped":
                    logger.info("OpenAI detected speech stop")
                    await self._send_phase(client_ws, "thinking")
                elif ev_type == "input_audio_buffer.committed":
                    logger.info("OpenAI audio buffer committed")
                else:
                    logger.debug("Unhandled OpenAI event: %s", ev_type)
        except websockets.exceptions.ConnectionClosed as e:
            end_reason["reason"] = "openai_disconnected"
            end_reason["code"] = e.code
            logger.info("openai_to_client ended (reason: connection_closed, code: %s)", e.code)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Error in openai_to_client: %s", e, exc_info=True)
            end_reason["reason"] = "openai_to_client_error"
        finally:
            for task in pending_tool_tasks:
                task.cancel()
            if pending_tool_tasks:
                await asyncio.gather(*pending_tool_tasks, return_exceptions=True)
            if "reason" not in end_reason:
                end_reason["reason"] = "openai_disconnected"

    async def _audio_sender(
        self,
        client_ws,
        audio_queue: asyncio.Queue,
        first_audio_to_client: list,
        end_reason: dict,
    ) -> None:
        """Send buffered audio to client at exactly the playback rate (token bucket)."""
        buffer = bytearray()
        bytes_sent = 0
        start_time: Optional[float] = None
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    if buffer:
                        await client_ws.send(bytes(buffer))
                        buffer.clear()
                    bytes_sent = 0
                    start_time = None
                    continue

                if not first_audio_to_client[0]:
                    first_audio_to_client[0] = True
                    logger.info("First audio to client (%d bytes)", len(chunk))

                buffer.extend(chunk)

                while len(buffer) >= SEND_CHUNK_SIZE:
                    to_send = bytes(buffer[:SEND_CHUNK_SIZE])
                    del buffer[:SEND_CHUNK_SIZE]

                    if start_time is None:
                        start_time = loop.time()

                    bytes_sent += len(to_send)
                    target_time = start_time + bytes_sent / BYTES_PER_SEC
                    now = loop.time()
                    if target_time > now:
                        await asyncio.sleep(target_time - now)

                    await client_ws.send(to_send)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            if "reason" not in end_reason:
                end_reason["reason"] = "client_disconnected"
        except Exception as e:
            logger.error("Error in audio_sender: %s", e, exc_info=True)
            if "reason" not in end_reason:
                end_reason["reason"] = "audio_sender_error"

    @staticmethod
    def _clear_audio_queue(audio_queue: asyncio.Queue) -> None:
        """Drop all pending audio so stale data is never sent after interrupt."""
        dropped = 0
        while not audio_queue.empty():
            try:
                audio_queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            logger.info("Cleared %d chunks from audio queue (interrupt)", dropped)

    async def _handle_tool_call(
        self,
        client_ws,
        openai_ws,
        item: dict,
        audio_queue: asyncio.Queue,
    ) -> bool:
        """Execute tool. Returns True if session should end (disconnect_client)."""
        name = item.get("name")
        call_id = item.get("call_id")
        args_str = item.get("arguments", "{}")
        if not call_id:
            return False
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}

        if name == "disconnect_client":
            logger.info("Disconnect tool called, notifying client")
            self._clear_audio_queue(audio_queue)
            try:
                await client_ws.send(
                    json.dumps({
                        "type": "disconnect",
                        "message": "User requested disconnect",
                        "reason": args.get("reason", "user_requested_stop"),
                    })
                )
            except Exception:
                pass
            return True

        if name == "search_web":
            query = args.get("query", "")
            if not query:
                await self._send_tool_result(
                    openai_ws, call_id, json.dumps({"error": "Missing query"})
                )
                return False
            try:
                text = await self._web_search(query)
                await self._send_tool_result(openai_ws, call_id, json.dumps({"result": text}))
            except Exception as e:
                logger.warning("Web search failed: %s", e)
                await self._send_tool_result(
                    openai_ws, call_id, json.dumps({"error": str(e)})
                )
            return False

        await self._send_tool_result(
            openai_ws, call_id, json.dumps({"error": f"Unknown tool: {name}"})
        )
        return False

    async def _run_tool_in_background(
        self,
        openai_ws,
        item: dict,
        response_idle: asyncio.Event,
    ) -> None:
        """Run a non-session-ending tool (e.g. search_web) without blocking the event loop.
        Waits for response_idle before sending tool result so we don't send response.create
        while the model is mid-speech."""
        name = item.get("name")
        call_id = item.get("call_id")
        if not call_id:
            return
        args_str = item.get("arguments", "{}")
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}

        output: str
        if name == "search_web":
            query = args.get("query", "")
            if not query:
                output = json.dumps({"error": "Missing query"})
            else:
                try:
                    text = await self._web_search(query)
                    output = json.dumps({"result": text})
                except Exception as e:
                    logger.warning("Web search failed: %s", e)
                    output = json.dumps({"error": str(e)})
        else:
            output = json.dumps({"error": f"Unknown tool: {name}"})

        await response_idle.wait()
        await self._send_tool_result(openai_ws, call_id, output)

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
                    "model": "gpt-5-nano",
                    "input": query,
                    "tools": [{"type": "web_search"}],
                },
                timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
        output = data.get("output", [])
        for item in output:
            if item.get("type") == "message":
                content = item.get("content", [])
                for part in content:
                    if part.get("type") == "output_text":
                        return part.get("text", "")
        return "No result from web search."

    async def handle_client(self, client_ws) -> None:
        """One client connection = one OpenAI session. Connect to OpenAI, send ready to
        client, then run forwarding tasks. Client only sends audio after ready."""
        client_id = self._client_id_from_ws(client_ws)
        logger.info("Client connected: %s", client_id)
        start_time = time.monotonic()

        if self.recorder:
            self.recorder.start_recording(client_id)

        end_reason = {}
        first_audio_from_client = [False]
        first_audio_to_client = [False]
        audio_queue: asyncio.Queue = asyncio.Queue()
        openai_ws = None

        try:
            openai_ws = await self._connect_openai()
            await self._wait_for_event(openai_ws, "session.created")
            await self._configure_session(openai_ws)
            await self._wait_for_event(openai_ws, "session.updated")
            await client_ws.send(json.dumps({"type": "ready"}))
            logger.info("Sent ready signal to client")

            response_idle = asyncio.Event()
            response_idle.set()
            pending_tool_tasks = set()
            client_task = asyncio.create_task(
                self._client_to_openai(
                    client_ws, openai_ws, client_id, audio_queue,
                    first_audio_from_client, end_reason,
                )
            )
            openai_task = asyncio.create_task(
                self._openai_to_client(
                    client_ws,
                    openai_ws,
                    client_id,
                    audio_queue,
                    end_reason,
                    response_idle,
                    pending_tool_tasks,
                )
            )
            sender_task = asyncio.create_task(
                self._audio_sender(
                    client_ws, audio_queue, first_audio_to_client, end_reason
                )
            )

            # #region agent log
            _dbg("H2", "handle:pre_wait", "all_tasks_created")
            # #endregion

            finished, pending = await asyncio.wait(
                [client_task, openai_task, sender_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # #region agent log
            task_names = {id(client_task): "client_to_openai", id(openai_task): "openai_to_client", id(sender_task): "audio_sender"}
            fin = [task_names.get(id(t), "?") for t in finished]
            pend = [task_names.get(id(t), "?") for t in pending]
            _dbg("H3", "handle:post_wait", "first_task_done", {"finished": fin, "pending": pend})
            # #endregion

            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

            for task in finished:
                if task.cancelled():
                    continue
                exc = task.exception()
                if exc is not None:
                    logger.error("Task exited with error: %s", exc)
        except Exception as e:
            logger.error("Session error: %s", e, exc_info=True)
            end_reason["reason"] = "session_error"
        finally:
            if openai_ws:
                try:
                    await openai_ws.close()
                except Exception:
                    pass
            if self.recorder:
                self.recorder.stop_recording()
            duration = time.monotonic() - start_time
            reason = end_reason.get("reason", "unknown")
            logger.info(
                "Session ended: %s (duration: %.1fs, reason: %s)",
                client_id, duration, reason,
            )

    async def run(self) -> None:
        """Start WebSocket server and run forever."""
        logger.info(
            "Starting WebSocket server on ws://%s:%s/",
            self.websocket_host, self.websocket_port,
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
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
