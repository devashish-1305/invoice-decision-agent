"""Thin wrapper around the Anthropic API. Every LLM call in the project goes through call().

What it guarantees:
  - structured output: the reply must validate against a Pydantic schema (messages.parse);
  - on a parse failure it retries ONCE (telling the model what was wrong), then raises LLMError, so the
    stage fails visibly instead of continuing with guessed data;
  - a 30 s timeout per request; the SDK itself retries transient failures (network errors, 429, 5xx) up to
    twice. That is separate from the one retry below, which is only for output that fails the schema;
  - the call log (client.calls) lets the pipeline and the scenario runner count LLM usage.

The API key is read from the environment (backend/.env) and is never logged or stored.
"""
import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from app.config import Settings

PROMPT_DIR = Path(__file__).parent / "prompts"
T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """The call failed (API error, refusal, or output that never matched the schema).
    str(error) is the technical detail (for logs and tests); error.user_message is the plain sentence shown in the UI."""

    def __init__(self, message: str, user_message: str = "The AI service didn't respond."):
        super().__init__(message)
        self.user_message = user_message


@dataclass
class LLMCall:
    prompt: str
    attempts: int
    duration_ms: int
    ok: bool
    input_tokens: int = 0     # from the API's usage report (0 if not reported)
    output_tokens: int = 0


def _plain(error: Exception) -> str:
    """The plain sentence for an Anthropic API failure."""
    if isinstance(error, anthropic.RateLimitError):
        return "The AI service is busy right now (rate limit reached)."
    if isinstance(error, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
        return "The AI service rejected the request (API key or access problem)."
    return "The AI service didn't respond."


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def image_block(png_bytes: bytes) -> dict:
    data = base64.standard_b64encode(png_bytes).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}


class LLMClient:
    def __init__(self, settings: Settings, sdk_client=None):
        self.settings = settings
        self._sdk_client = sdk_client          # injectable so tests never need an API key
        self.calls: list[LLMCall] = []

    def _sdk(self):
        if self._sdk_client is None:
            if self.settings.llm_provider != "anthropic":
                raise LLMError(f"Unsupported LLM_PROVIDER '{self.settings.llm_provider}' (only 'anthropic')",
                               "The AI service isn't set up correctly.")
            key = os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise LLMError("ANTHROPIC_API_KEY is not set (add it to backend/.env)",
                               "The AI service isn't set up: no API key is configured.")
            self._sdk_client = anthropic.Anthropic(api_key=key, timeout=self.settings.llm_timeout_s, max_retries=2)
        return self._sdk_client

    def call(self, prompt: str, content: list[dict], schema: type[T]) -> T:
        """Send `content` (text/image blocks) under prompts/<prompt>.md; return a validated `schema`."""
        system = (PROMPT_DIR / f"{prompt}.md").read_text(encoding="utf-8")
        started = time.perf_counter()
        problem = None
        for attempt in (1, 2):
            blocks = content if problem is None else content + [text_block(
                f"Your previous reply was rejected: {problem}\nReply again, matching the schema exactly.")]
            try:
                response = self._sdk().messages.parse(
                    model=self.settings.llm_model, max_tokens=self.settings.llm_max_tokens, system=system,
                    messages=[{"role": "user", "content": blocks}], output_format=schema,
                    output_config={"effort": self.settings.llm_effort})
            except ValueError as e:            # pydantic.ValidationError and bad JSON are both ValueErrors
                problem = str(e)[:600]
                continue
            except anthropic.APIError as e:    # timeout, connection, auth, rate limit, 5xx ...
                self._log(prompt, attempt, started, ok=False)
                raise LLMError(f"{prompt}: Anthropic API error: {type(e).__name__}: {e}", _plain(e)) from e
            if response.stop_reason == "refusal":
                self._log(prompt, attempt, started, ok=False)
                raise LLMError(f"{prompt}: the model declined to answer", "The AI declined to read this document.")
            if response.parsed_output is None:
                problem = f"no parsable JSON (stop_reason={response.stop_reason})"
                continue
            usage = getattr(response, "usage", None)
            self._log(prompt, attempt, started, ok=True, input_tokens=getattr(usage, "input_tokens", 0) or 0,
                      output_tokens=getattr(usage, "output_tokens", 0) or 0)
            return response.parsed_output
        self._log(prompt, 2, started, ok=False)
        raise LLMError(f"{prompt}: reply did not match the schema after one retry: {problem}",
                       "The AI's answer couldn't be used.")

    def _log(self, prompt, attempts, started, ok, input_tokens=0, output_tokens=0):
        self.calls.append(LLMCall(prompt, attempts, int((time.perf_counter() - started) * 1000), ok,
                                  input_tokens, output_tokens))
