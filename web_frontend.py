from __future__ import annotations

import json
import os
import threading
import uuid
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from chatbot import AIChatBot, build_chatbot_from_env


STATIC_DIR = Path(__file__).with_name("static")
SESSION_COOKIE_NAME = "chatbot_session"


class BotStore:
    def __init__(self, factory: Callable[[], AIChatBot]) -> None:
        self._factory = factory
        self._bots: dict[str, AIChatBot] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> AIChatBot:
        with self._lock:
            if session_id not in self._bots:
                self._bots[session_id] = self._factory()
            return self._bots[session_id]

    def reset(self, session_id: str) -> AIChatBot:
        with self._lock:
            self._bots[session_id] = self._factory()
            return self._bots[session_id]


def _default_bot_factory() -> AIChatBot:
    bot, _ = build_chatbot_from_env(token_handler=lambda _: None)
    return bot


def get_server_settings() -> tuple[str, int]:
    raw_port = os.getenv("CHATBOT_PORT") or os.getenv("PORT") or "8000"
    port = int(raw_port)

    host = os.getenv("CHATBOT_HOST") or os.getenv("HOST")
    if not host:
        host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"

    return host, port


def create_handler(
    bot_store: BotStore,
    *,
    static_dir: Path = STATIC_DIR,
    app_title: str = "Laser ai",
) -> type[BaseHTTPRequestHandler]:
    class ChatHandler(BaseHTTPRequestHandler):
        server_version = "LaserAI/1.0"

        def _session_id(self) -> tuple[str, bool]:
            cookie_header = self.headers.get("Cookie")
            if cookie_header:
                cookie = SimpleCookie()
                cookie.load(cookie_header)
                morsel = cookie.get(SESSION_COOKIE_NAME)
                if morsel and morsel.value:
                    return morsel.value, False
            return uuid.uuid4().hex, True

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length", "0"))
            payload = self.rfile.read(length) if length else b"{}"
            if not payload:
                return {}
            return json.loads(payload.decode("utf-8"))

        def _send_bytes(
            self,
            body: bytes,
            *,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
            session_id: str | None = None,
            set_cookie: bool = False,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if set_cookie and session_id:
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE_NAME}={session_id}; Path=/; HttpOnly; SameSite=Lax",
                )
            self.end_headers()
            self.wfile.write(body)

        def _send_json(
            self,
            payload: dict,
            *,
            status: HTTPStatus = HTTPStatus.OK,
            session_id: str | None = None,
            set_cookie: bool = False,
        ) -> None:
            self._send_bytes(
                json.dumps(payload).encode("utf-8"),
                content_type="application/json; charset=utf-8",
                status=status,
                session_id=session_id,
                set_cookie=set_cookie,
            )

        def _serve_static(self, relative_path: str, *, session_id: str, set_cookie: bool) -> None:
            target = (static_dir / relative_path).resolve()
            try:
                target.relative_to(static_dir.resolve())
            except ValueError:
                self._send_json({"error": "File not found."}, status=HTTPStatus.NOT_FOUND)
                return

            if not target.exists() or not target.is_file():
                self._send_json({"error": "File not found."}, status=HTTPStatus.NOT_FOUND)
                return

            content_type = {
                ".html": "text/html; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".json": "application/json; charset=utf-8",
            }.get(target.suffix.lower(), "application/octet-stream")

            content = target.read_bytes()
            if target.suffix.lower() == ".html":
                content = content.replace(b"{{APP_TITLE}}", app_title.encode("utf-8"))

            self._send_bytes(
                content,
                content_type=content_type,
                session_id=session_id,
                set_cookie=set_cookie,
            )

        def do_GET(self) -> None:
            session_id, set_cookie = self._session_id()
            parsed = urlparse(self.path)

            if parsed.path == "/":
                self._serve_static("index.html", session_id=session_id, set_cookie=set_cookie)
                return

            if parsed.path.startswith("/static/"):
                relative_path = parsed.path.removeprefix("/static/")
                self._serve_static(relative_path, session_id=session_id, set_cookie=set_cookie)
                return

            if parsed.path == "/api/health":
                self._send_json(
                    {"status": "ok", "app": app_title},
                    session_id=session_id,
                    set_cookie=set_cookie,
                )
                return

            self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            session_id, set_cookie = self._session_id()
            parsed = urlparse(self.path)

            try:
                data = self._read_json()
            except json.JSONDecodeError:
                self._send_json(
                    {"error": "Invalid JSON payload."},
                    status=HTTPStatus.BAD_REQUEST,
                    session_id=session_id,
                    set_cookie=set_cookie,
                )
                return

            if parsed.path == "/api/chat":
                message = str(data.get("message", "")).strip()
                if not message:
                    self._send_json(
                        {"error": "Message is required."},
                        status=HTTPStatus.BAD_REQUEST,
                        session_id=session_id,
                        set_cookie=set_cookie,
                    )
                    return

                bot = bot_store.get(session_id)
                command_result = bot.handle_command(message)
                if command_result == "__quit__":
                    bot_store.reset(session_id)
                    self._send_json(
                        {"reply": "Session ended. Start a new chat whenever you're ready."},
                        session_id=session_id,
                        set_cookie=set_cookie,
                    )
                    return
                if command_result is not None:
                    self._send_json(
                        {"reply": command_result, "command": True},
                        session_id=session_id,
                        set_cookie=set_cookie,
                    )
                    return

                try:
                    reply = bot.chat(message, stream=False)
                except RuntimeError as exc:
                    self._send_json(
                        {"error": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                        session_id=session_id,
                        set_cookie=set_cookie,
                    )
                    return

                self._send_json(
                    {"reply": reply, "command": False},
                    session_id=session_id,
                    set_cookie=set_cookie,
                )
                return

            if parsed.path == "/api/reset":
                bot_store.reset(session_id)
                self._send_json(
                    {"reply": "Conversation cleared."},
                    session_id=session_id,
                    set_cookie=set_cookie,
                )
                return

            self._send_json({"error": "Not found."}, status=HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args) -> None:
            return

    return ChatHandler


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    handler = create_handler(BotStore(_default_bot_factory))
    with ThreadingHTTPServer((host, port), handler) as server:
        print(f"Laser ai is running at http://{host}:{port}")
        print("Press Ctrl+C to stop the server.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


if __name__ == "__main__":
    server_host, server_port = get_server_settings()
    run_server(server_host, server_port)
