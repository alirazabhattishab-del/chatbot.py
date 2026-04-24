from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


DEFAULT_PROMPT_FILE = Path(__file__).with_name("system_prompt.txt")
DEFAULT_TRANSCRIPTS_DIR = Path(__file__).with_name("transcripts")
DEFAULT_ENV_FILE = Path(__file__).with_name(".env")
HELP_TEXT = (
    "Commands:\n"
    "/help  Show commands\n"
    "/reset Start a new conversation\n"
    "/save  Save the current transcript to a Markdown file\n"
    "/quit  Exit the chatbot"
)


def _parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _load_dotenv(path: Path = DEFAULT_ENV_FILE) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _build_openai_client(api_key: str) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The 'openai' package is not installed. Run 'pip install -r requirements.txt'."
        ) from exc

    return OpenAI(api_key=api_key)


@dataclass(slots=True)
class ChatBotConfig:
    api_key: str
    model: str = "gpt-5.4-mini"
    reasoning_effort: str = "medium"
    max_output_tokens: int = 900
    stream: bool = True
    store: bool = True
    prompt_file: Path = DEFAULT_PROMPT_FILE
    transcripts_dir: Path = DEFAULT_TRANSCRIPTS_DIR

    @classmethod
    def from_env(cls) -> "ChatBotConfig":
        _load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is missing. Add it to your shell or a local .env file before running the chatbot."
            )

        return cls(
            api_key=api_key,
            model=os.getenv("OPENAI_MODEL", "gpt-5.4-mini"),
            reasoning_effort=os.getenv("OPENAI_REASONING_EFFORT", "medium"),
            max_output_tokens=int(os.getenv("CHATBOT_MAX_OUTPUT_TOKENS", "900")),
            stream=_parse_bool(os.getenv("CHATBOT_STREAM"), default=True),
            store=_parse_bool(os.getenv("CHATBOT_STORE"), default=True),
        )


@dataclass(slots=True)
class TranscriptEntry:
    role: str
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AIChatBot:
    def __init__(
        self,
        config: ChatBotConfig,
        *,
        client: Any | None = None,
        token_handler: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config
        self.client = client or _build_openai_client(config.api_key)
        self.instructions = self._load_prompt(config.prompt_file)
        self.previous_response_id: str | None = None
        self.transcript: list[TranscriptEntry] = []
        self.token_handler = token_handler or (lambda text: print(text, end="", flush=True))

    def _load_prompt(self, path: Path) -> str:
        if not path.exists():
            raise RuntimeError(f"Missing prompt file: {path}")
        return path.read_text(encoding="utf-8").strip()

    def _extract_text(self, response: Any) -> str:
        if response is None:
            return ""

        output_text = getattr(response, "output_text", None)
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        output = getattr(response, "output", None)
        if output is None and isinstance(response, dict):
            output = response.get("output", [])

        collected: list[str] = []
        for item in output or []:
            content = getattr(item, "content", None)
            if content is None and isinstance(item, dict):
                content = item.get("content", [])
            for block in content or []:
                block_type = getattr(block, "type", None)
                if block_type is None and isinstance(block, dict):
                    block_type = block.get("type")
                if block_type == "output_text":
                    text = getattr(block, "text", None)
                    if text is None and isinstance(block, dict):
                        text = block.get("text", "")
                    collected.append(text or "")

        return "".join(collected).strip()

    def _request_payload(self, user_message: str, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "instructions": self.instructions,
            "input": [{"role": "user", "content": user_message}],
            "max_output_tokens": self.config.max_output_tokens,
            "reasoning": {"effort": self.config.reasoning_effort},
            "store": self.config.store,
            "truncation": "auto",
            "stream": stream,
        }
        if self.previous_response_id:
            payload["previous_response_id"] = self.previous_response_id
        return payload

    def reset(self) -> None:
        self.previous_response_id = None
        self.transcript.clear()

    def handle_command(self, raw_message: str) -> str | None:
        if not raw_message.startswith("/"):
            return None

        command, _, argument = raw_message.partition(" ")
        normalized = command.lower()

        if normalized in {"/quit", "/exit"}:
            return "__quit__"
        if normalized == "/help":
            return HELP_TEXT
        if normalized == "/reset":
            self.reset()
            return "Conversation cleared."
        if normalized == "/save":
            target = Path(argument.strip()) if argument.strip() else None
            saved_path = self.save_transcript(target)
            return f"Transcript saved to {saved_path}."
        return "Unknown command. Use /help."

    def save_transcript(self, path: Path | None = None) -> Path:
        self.config.transcripts_dir.mkdir(parents=True, exist_ok=True)
        target = path or self.config.transcripts_dir / (
            f"chat-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
        )
        if not target.is_absolute():
            target = Path.cwd() / target

        lines = ["# Chat Transcript", ""]
        for entry in self.transcript:
            timestamp = entry.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
            lines.append(f"## {entry.role.title()} ({timestamp})")
            lines.append("")
            lines.append(entry.content)
            lines.append("")

        target.write_text("\n".join(lines), encoding="utf-8")
        return target.resolve()

    def chat(self, user_message: str, *, stream: bool | None = None) -> str:
        message = user_message.strip()
        if not message:
            return "Please type a message so I can help."

        use_stream = self.config.stream if stream is None else stream
        self.transcript.append(TranscriptEntry(role="user", content=message))

        if use_stream:
            reply, response_id = self._stream_response(message)
        else:
            reply, response_id = self._non_stream_response(message)

        self.previous_response_id = response_id or self.previous_response_id
        self.transcript.append(TranscriptEntry(role="assistant", content=reply))
        return reply

    def _non_stream_response(self, user_message: str) -> tuple[str, str | None]:
        response = self.client.responses.create(**self._request_payload(user_message, stream=False))
        reply = self._extract_text(response)
        if not reply:
            reply = "I could not generate a response. Please try again."
        return reply, getattr(response, "id", None)

    def _stream_response(self, user_message: str) -> tuple[str, str | None]:
        stream = self.client.responses.create(**self._request_payload(user_message, stream=True))
        parts: list[str] = []
        final_response: Any | None = None

        for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    parts.append(delta)
                    self.token_handler(delta)
            elif event_type == "response.completed":
                final_response = getattr(event, "response", None)
            elif event_type == "error":
                raise RuntimeError(getattr(event, "message", "Streaming failed."))

        if final_response is None and hasattr(stream, "get_final_response"):
            final_response = stream.get_final_response()

        reply = self._extract_text(final_response) or "".join(parts).strip()
        if not reply:
            reply = "I could not generate a response. Please try again."
        response_id = getattr(final_response, "id", None)
        return reply, response_id


def build_chatbot_from_env(
    *,
    token_handler: Callable[[str], None] | None = None,
) -> tuple[AIChatBot, ChatBotConfig]:
    config = ChatBotConfig.from_env()
    bot = AIChatBot(config, token_handler=token_handler)
    return bot, config


def main() -> None:
    try:
        bot, config = build_chatbot_from_env()
    except RuntimeError as exc:
        print(exc)
        return

    print(f"AI ChatBot ready on model {config.model}. Type /help for commands.")

    while True:
        try:
            user_message = input("You: ").strip()
        except EOFError:
            print("\nBot: Goodbye!")
            break

        command_result = bot.handle_command(user_message)
        if command_result == "__quit__":
            print("Bot: Goodbye!")
            break
        if command_result is not None:
            print(f"Bot: {command_result}")
            continue

        if not user_message:
            print("Bot: Please type a message so I can help.")
            continue

        print("Bot: ", end="", flush=True)
        reply = bot.chat(user_message)
        if config.stream:
            print()
        else:
            print(reply)


if __name__ == "__main__":
    main()
