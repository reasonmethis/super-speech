from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Literal, Protocol

CONTROL_ENDPOINT_FILENAME = "control.json"
CONTROL_PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 16 * 1024 * 1024

ControlHandler = Callable[[dict[str, object]], object]
PlaybackState = Literal["idle", "paused", "playing"]


class PauseablePlayback(Protocol):
    def set_paused(self, paused: bool) -> bool: ...


class LivePlaybackControl:
    """Apply desktop controls to live audio before persisting their markers."""

    def __init__(self, read_persisted_pause: Callable[[], bool]) -> None:
        self._read_persisted_pause = read_persisted_pause
        self._lock = threading.Lock()
        self._playback: PauseablePlayback | None = None
        self._command: tuple[object, bool] | None = None
        self._clear_owner: str | None = None
        self._silenced_playback: PauseablePlayback | None = None

    def pause_requested(self) -> bool:
        with self._lock:
            return self._pause_intent()

    def begin_command(self, paused: bool) -> tuple[object, PlaybackState]:
        """Apply a command and return its ownership token and live audio state."""
        with self._lock:
            if self._clear_blocks_playback():
                raise RuntimeError("playback cannot change while Clear is finishing")
            token = object()
            self._command = (token, paused)
            return token, self._set_playback_paused(paused)

    def end_command(self, token: object) -> None:
        """Return control to the marker if this command still owns playback."""
        with self._lock:
            if self._command is None or self._command[0] is not token:
                return
            self._command = None
            self._set_playback_paused(self._pause_intent())

    def start_clearing(self, request_id: str) -> None:
        """Keep live audio silent until the Clear transaction settles."""
        with self._lock:
            if self._clear_owner not in {None, request_id}:
                raise RuntimeError("another Clear request is already in progress")
            self._clear_owner = request_id
            self._set_playback_paused(True)

    def finish_clearing(self, request_id: str, *, hold_active: bool) -> None:
        """Finish Clear, optionally keeping its old stream silent until detach."""
        with self._lock:
            if self._clear_owner != request_id:
                return
            self._clear_owner = None
            self._silenced_playback = self._playback if hold_active else None
            self._set_playback_paused(self._pause_intent())

    def attach(self, playback: PauseablePlayback) -> bool:
        """Expose one audio stream to live controls and apply current intent."""
        with self._lock:
            self._playback = playback
            paused = self._pause_intent()
            playback.set_paused(paused)
            return paused

    def detach(self, playback: PauseablePlayback) -> None:
        with self._lock:
            if self._playback is playback:
                self._playback = None
                if self._silenced_playback is playback:
                    self._silenced_playback = None

    def _pause_intent(self) -> bool:
        if self._clear_blocks_playback():
            return True
        if self._command is not None:
            return self._command[1]
        return self._read_persisted_pause()

    def _clear_blocks_playback(self) -> bool:
        return self._clear_owner is not None or (
            self._playback is not None and self._silenced_playback is self._playback
        )

    def _set_playback_paused(self, paused: bool) -> PlaybackState:
        if self._playback is None:
            return "idle"
        self._playback.set_paused(paused)
        return "paused" if paused else "playing"


class _ControlHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    token: str
    control_handler: ControlHandler


class _ControlRequestHandler(BaseHTTPRequestHandler):
    server: _ControlHttpServer

    def do_POST(self) -> None:
        if self.path != "/v1/control":
            self._send(404, {"error": "unknown engine control endpoint"})
            return
        supplied_token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not hmac.compare_digest(supplied_token, self.server.token):
            self._send(401, {"error": "invalid engine control token"})
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._send(400, {"error": "invalid engine control request size"})
            return
        try:
            payload = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "invalid engine control JSON"})
            return
        if not isinstance(payload, dict):
            self._send(400, {"error": "invalid engine control request"})
            return
        try:
            result = self.server.control_handler(payload)
        except ValueError as error:
            self._send(400, {"error": str(error)})
            return
        except RuntimeError as error:
            self._send(409, {"error": str(error)})
            return
        self._send(200, {"result": result})

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class EngineControlServer:
    """Expose one authenticated control endpoint for the running engine."""

    def __init__(
        self,
        base: Path,
        engine_pid: int,
        control_handler: ControlHandler,
    ) -> None:
        self.endpoint_path = base / CONTROL_ENDPOINT_FILENAME
        self.engine_pid = engine_pid
        self.control_handler = control_handler
        self._server: _ControlHttpServer | None = None

    def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("engine control server is already running")
        token = secrets.token_hex(32)
        server = _ControlHttpServer(("127.0.0.1", 0), _ControlRequestHandler)
        server.token = token
        server.control_handler = self.control_handler
        threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="super-speech-control",
            daemon=True,
        ).start()
        try:
            self._publish_endpoint(server.server_port, token)
        except Exception:
            server.shutdown()
            server.server_close()
            raise
        self._server = server

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._remove_owned_endpoint(server.token)
        server.shutdown()
        server.server_close()
        self._server = None

    def _publish_endpoint(self, port: int, token: str) -> None:
        self.endpoint_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": CONTROL_PROTOCOL_VERSION,
            "engine_pid": self.engine_pid,
            "port": port,
            "token": token,
        }
        temporary = self.endpoint_path.with_name(
            f".{self.endpoint_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, separators=(",", ":")),
                encoding="utf-8",
            )
            if os.name != "nt":
                temporary.chmod(0o600)
            os.replace(temporary, self.endpoint_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _remove_owned_endpoint(self, token: str) -> None:
        try:
            payload = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return
        if isinstance(payload, dict) and payload.get("token") == token:
            self.endpoint_path.unlink(missing_ok=True)
