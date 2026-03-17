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
        self.voice = os.environ.get("VOICE", "marin")
        self.realtime_model = os.environ.get("REALTIME_MODEL", "gpt-realtime-mini")
        self.web_search_model = os.environ.get("WEB_SEARCH_MODEL", "gpt-4.1-mini")
        self.enable_recording = os.environ.get("ENABLE_RECORDING", "false").lower() == "true"
        self.recorder: Optional[AudioRecorder] = None
        if self.enable_recording:
            self.recorder = AudioRecorder(output_dir="recordings")

        self._active_sessions: dict[str, websockets.WebSocketServerProtocol] = {}

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
                        "voice": self.voice,
                    },
                },
                "tools": TOOLS,
            },
        }

    async def _connect_openai(self):
        """Open WebSocket to OpenAI Realtime API."""
        default_url = f"wss://api.openai.com/v1/realtime?model={self.realtime_model}"
        url = os.environ.get("OPENAI_REALTIME_URL", default_url)
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
        output_queue: asyncio.Queue,
        first_audio_from_client: list,
        end_reason: dict,
    ) -> None:
        """Forward ESP32 binary audio and JSON control to OpenAI. Client only sends audio
        after receiving ready, so openai_ws is always valid here."""
        try:
            async for message in client_ws:
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
                            self._clear_output_queue(output_queue)
                            await openai_ws.send(json.dumps({"type": "response.cancel"}))
                    except (json.JSONDecodeError, TypeError):
                        pass
        except websockets.exceptions.ConnectionClosed as e:
            end_reason["reason"] = "client_disconnected"
            end_reason["code"] = e.code
            logger.info("client_to_openai ended (reason: connection_closed, code: %s)", e.code)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Error in client_to_openai: %s", e, exc_info=True)
            end_reason["reason"] = "client_to_openai_error"
        finally:
            if "reason" not in end_reason:
                end_reason["reason"] = "client_disconnected"

    async def _send_phase(self, client_ws, phase: str) -> None:
        """Send phase message to client for LED/UX feedback."""
        try:
            await client_ws.send(json.dumps({"type": "phase", "phase": phase}))
            logger.info("Phase -> %s", phase)
        except Exception as e:
            logger.warning("Failed to send phase '%s': %s", phase, e)

    async def _openai_to_client(
        self,
        client_ws,
        openai_ws,
        client_id: str,
        output_queue: asyncio.Queue,
        end_reason: dict,
        response_idle: asyncio.Event,
        pending_tool_tasks: set,
        in_flight_searches: dict,
    ) -> None:
        """Forward OpenAI events to ESP32; audio and phases go through single output queue.
        Sends phase messages (thinking/replying/searching) for client LED feedback."""
        delta_count = 0
        delta_bytes_total = 0
        sent_replying_phase = False
        fatal_error_sent = False
        try:
            async for raw in openai_ws:
                if not isinstance(raw, str):
                    continue
                if fatal_error_sent:
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
                            await output_queue.put(("phase", "replying"))
                        audio_bytes = base64.standard_b64decode(delta_b64)
                        await output_queue.put(audio_bytes)
                        delta_count += 1
                        delta_bytes_total += len(audio_bytes)
                        if self.recorder:
                            self.recorder.record_output_audio(audio_bytes)

                elif ev_type == "response.output_audio.done":
                    await output_queue.put(None)
                    sent_replying_phase = False
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
                        if status == "failed":
                            details = response.get("status_details")
                            is_quota = False
                            if isinstance(details, dict):
                                error = details.get("error", {})
                                if isinstance(error, dict):
                                    is_quota = error.get("type") == "insufficient_quota"
                                elif isinstance(error, str):
                                    is_quota = "insufficient_quota" in error
                            elif isinstance(details, str):
                                is_quota = "insufficient_quota" in details
                            if is_quota:
                                logger.error("OpenAI quota exceeded — notifying client")
                                await output_queue.put(("error", "insufficient_quota"))
                                fatal_error_sent = True
                    output = response.get("output", [])
                    output_summary = [
                        item.get("type") + (f"({item.get('name', '')})" if item.get("type") == "function_call" else "")
                        for item in output
                    ]
                    if output_summary:
                        logger.info("Response output: %s", output_summary)
                    for item in output:
                        if item.get("type") == "function_call":
                            if item.get("name") == "disconnect_client":
                                should_stop = await self._handle_tool_call(
                                    client_ws, openai_ws, item, output_queue
                                )
                                if should_stop:
                                    end_reason["reason"] = "disconnect_tool"
                                    return
                            else:
                                task = asyncio.create_task(
                                    self._run_tool_in_background(
                                        openai_ws, item, response_idle, in_flight_searches
                                    )
                                )
                                pending_tool_tasks.add(task)
                                task.add_done_callback(pending_tool_tasks.discard)
                    if pending_tool_tasks:
                        logger.info("Phase decision: pending_tools=%d -> searching", len(pending_tool_tasks))
                        await output_queue.put(("phase", "searching"))

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
                    await output_queue.put(("phase", "thinking"))
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

    async def _output_sender(
        self,
        client_ws,
        output_queue: asyncio.Queue,
        first_audio_to_client: list,
        end_reason: dict,
    ) -> None:
        """Send buffered audio and phase messages to client. Audio paced at playback rate;
        phases sent in order. Single ordered stream eliminates race conditions."""
        buffer = bytearray()
        bytes_sent = 0
        start_time: Optional[float] = None
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(output_queue.get(), timeout=0.1)
                except asyncio.TimeoutError:
                    continue

                if chunk is None:
                    if buffer:
                        await client_ws.send(bytes(buffer))
                        buffer.clear()
                    bytes_sent = 0
                    start_time = None
                    continue

                if isinstance(chunk, tuple) and chunk[0] == "phase":
                    if buffer:
                        await client_ws.send(bytes(buffer))
                        buffer.clear()
                    await self._send_phase(client_ws, chunk[1])
                    continue

                if isinstance(chunk, tuple) and chunk[0] == "error":
                    if buffer:
                        await client_ws.send(bytes(buffer))
                        buffer.clear()
                    error_code = chunk[1]
                    try:
                        await client_ws.send(json.dumps({"type": "error", "code": error_code}))
                        logger.info("Sent error to client: code=%s", error_code)
                    except Exception as e:
                        logger.warning("Failed to send error '%s': %s", error_code, e)
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
            logger.error("Error in output_sender: %s", e, exc_info=True)
            if "reason" not in end_reason:
                end_reason["reason"] = "output_sender_error"

    @staticmethod
    def _clear_output_queue(output_queue: asyncio.Queue) -> None:
        """Drop all pending output (audio + phases) so stale data is never sent after interrupt."""
        dropped = 0
        while not output_queue.empty():
            try:
                output_queue.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            logger.info("Cleared %d chunks from output queue (interrupt)", dropped)

    async def _handle_tool_call(
        self,
        client_ws,
        openai_ws,
        item: dict,
        output_queue: asyncio.Queue,
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
            self._clear_output_queue(output_queue)
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

        return False

    async def _run_tool_in_background(
        self,
        openai_ws,
        item: dict,
        response_idle: asyncio.Event,
        in_flight_searches: dict,
    ) -> None:
        """Run a non-session-ending tool (e.g. search_web) without blocking the event loop.
        Waits for response_idle before sending tool result so we don't send response.create
        while the model is mid-speech. Deduplicates in-flight search_web by query."""
        name = item.get("name")
        call_id = item.get("call_id")
        if not call_id:
            return
        args_str = item.get("arguments", "{}")
        try:
            args = json.loads(args_str) if args_str else {}
        except json.JSONDecodeError:
            args = {}

        logger.info("Tool call: name=%s", name)
        logger.debug("Tool call args: %s", args)

        output: str
        if name == "search_web":
            query = args.get("query", "")
            if not query:
                output = json.dumps({"error": "Missing query"})
                logger.warning("search_web called with empty query")
            else:
                dedup_key = f"{name}:{query}"
                if dedup_key in in_flight_searches:
                    logger.info("Dedup: reusing in-flight search for '%s'", query)
                    output = await in_flight_searches[dedup_key]
                    await response_idle.wait()
                    await self._send_tool_result_output_only(openai_ws, call_id, output)
                    return
                future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
                in_flight_searches[dedup_key] = future
                try:
                    search_start = time.monotonic()
                    try:
                        text = await self._web_search(query)
                        duration_s = time.monotonic() - search_start
                        output = json.dumps({"result": text})
                        logger.info(
                            "Web search completed in %.1fs, result %d chars",
                            duration_s, len(text),
                        )
                    except Exception as e:
                        duration_s = time.monotonic() - search_start
                        err_msg = f"{type(e).__name__}: {e!s}" if str(e) else type(e).__name__
                        if isinstance(e, httpx.HTTPStatusError):
                            try:
                                body = e.response.text
                                err_msg = f"{err_msg} body=%s" % (
                                    body[:200] + "…" if len(body) > 200 else body
                                )
                            except Exception:
                                pass
                        logger.warning(
                            "Web search failed after %.1fs: %s", duration_s, err_msg,
                            exc_info=True,
                        )
                        output = json.dumps({"error": str(e) or type(e).__name__})
                    future.set_result(output)
                finally:
                    in_flight_searches.pop(dedup_key, None)
        else:
            output = json.dumps({"error": f"Unknown tool: {name}"})

        await response_idle.wait()
        result_preview = "ok" if "error" not in output else "error"
        logger.info(
            "Sending tool result: call_id=%s %s",
            call_id[:16] + "…" if len(call_id) > 16 else call_id,
            result_preview,
        )
        await self._send_tool_result(openai_ws, call_id, output)

    async def _send_tool_result(self, openai_ws, call_id: str, output: str) -> None:
        """Send function_call_output item and trigger response.create."""
        await self._send_tool_result_output_only(openai_ws, call_id, output)
        await openai_ws.send(json.dumps({"type": "response.create"}))

    async def _send_tool_result_output_only(
        self, openai_ws, call_id: str, output: str
    ) -> None:
        """Send only the function_call_output item (no response.create). Used for dedup."""
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
                    "model": self.web_search_model,
                    "input": query,
                    "tools": [{"type": "web_search"}],
                },
                timeout=45.0,
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

        old_ws = self._active_sessions.get(client_id)
        if old_ws is not None:
            logger.warning("Closing previous session for %s", client_id)
            try:
                await old_ws.close(1000, "replaced by new connection")
            except Exception:
                pass
        self._active_sessions[client_id] = client_ws

        if self.recorder:
            self.recorder.start_recording(client_id)

        end_reason = {}
        first_audio_from_client = [False]
        first_audio_to_client = [False]
        output_queue: asyncio.Queue = asyncio.Queue()
        openai_ws = None

        try:
            openai_ws = await self._connect_openai()
            await self._wait_for_event(openai_ws, "session.created")
            await self._configure_session(openai_ws)
            await self._wait_for_event(openai_ws, "session.updated")
            instructions_preview = (self.instructions[:100] + "…") if len(self.instructions) > 100 else self.instructions
            logger.info("Instructions: %s", instructions_preview)
            await client_ws.send(json.dumps({"type": "ready"}))
            logger.info("Sent ready signal to client")

            response_idle = asyncio.Event()
            response_idle.set()
            pending_tool_tasks = set()
            in_flight_searches: dict[str, asyncio.Future[str]] = {}

            client_task = asyncio.create_task(
                self._client_to_openai(
                    client_ws, openai_ws, client_id, output_queue,
                    first_audio_from_client, end_reason,
                )
            )
            openai_task = asyncio.create_task(
                self._openai_to_client(
                    client_ws,
                    openai_ws,
                    client_id,
                    output_queue,
                    end_reason,
                    response_idle,
                    pending_tool_tasks,
                    in_flight_searches,
                )
            )
            sender_task = asyncio.create_task(
                self._output_sender(
                    client_ws, output_queue, first_audio_to_client, end_reason
                )
            )
            finished, pending = await asyncio.wait(
                [client_task, openai_task, sender_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

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
            if self._active_sessions.get(client_id) is client_ws:
                del self._active_sessions[client_id]
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
