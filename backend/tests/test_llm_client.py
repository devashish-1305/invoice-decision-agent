"""The LLM wrapper, tested with a fake SDK: structured output, one retry on parse failure, fail visibly."""
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.llm.client import LLMClient, LLMError
from app.schemas import Explanation

GOOD = Explanation(summary="ok", drafted_message=None)


def validation_error():
    try:
        Explanation.model_validate({})
    except ValidationError as e:
        return e


def reply(parsed=GOOD, stop_reason="end_turn"):
    return SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason)


class FakeSDK:
    """messages.parse() returns / raises the queued items in order."""
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []
        self.messages = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client(*outcomes):
    sdk = FakeSDK(*outcomes)
    return LLMClient(Settings(), sdk_client=sdk), sdk


def test_valid_reply_is_returned_after_one_attempt():
    llm, sdk = client(reply())
    assert llm.call("explain", [], Explanation) == GOOD
    assert len(sdk.requests) == 1 and llm.calls[0].attempts == 1 and llm.calls[0].ok


def test_request_uses_structured_output_with_the_schema_and_configured_model():
    llm, sdk = client(reply())
    llm.call("explain", [{"type": "text", "text": "hi"}], Explanation)
    sent = sdk.requests[0]
    assert sent["output_format"] is Explanation and sent["model"] == Settings().llm_model
    assert "ignore" in sent["system"].lower()  # the prompt tells the model to ignore instructions in the document


def test_parse_failure_is_retried_once_then_succeeds():
    llm, sdk = client(validation_error(), reply())
    assert llm.call("explain", [], Explanation) == GOOD
    assert len(sdk.requests) == 2 and llm.calls[0].attempts == 2
    assert "rejected" in sdk.requests[1]["messages"][0]["content"][-1]["text"]  # tells the model what was wrong


def test_two_parse_failures_fail_the_stage_instead_of_guessing():
    llm, sdk = client(validation_error(), validation_error())
    with pytest.raises(LLMError):
        llm.call("explain", [], Explanation)
    assert len(sdk.requests) == 2 and not llm.calls[0].ok


def test_empty_parsed_output_counts_as_a_parse_failure():
    llm, sdk = client(reply(parsed=None, stop_reason="max_tokens"), reply(parsed=None, stop_reason="max_tokens"))
    with pytest.raises(LLMError):
        llm.call("explain", [], Explanation)


def test_api_errors_are_not_retried():
    timeout = anthropic.APITimeoutError(request=httpx2.Request("POST", "https://example.invalid"))
    llm, sdk = client(timeout)
    with pytest.raises(LLMError, match="APITimeoutError"):
        llm.call("explain", [], Explanation)
    assert len(sdk.requests) == 1


def test_refusal_fails_the_stage():
    llm, _ = client(reply(parsed=None, stop_reason="refusal"))
    with pytest.raises(LLMError, match="declined"):
        llm.call("explain", [], Explanation)


def test_missing_api_key_fails_clearly(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        LLMClient(Settings()).call("explain", [], Explanation)


def test_every_prompt_tells_the_model_to_ignore_instructions_inside_the_document():
    from app.llm.client import PROMPT_DIR
    for name in ("extract", "infer_po", "match_lines", "explain"):
        assert "ignore" in (PROMPT_DIR / f"{name}.md").read_text().lower(), name


def test_extraction_prompt_demands_null_for_anything_not_visible():
    from app.llm.client import PROMPT_DIR
    text = (PROMPT_DIR / "extract.md").read_text()
    assert "null" in text and "evidence" in text and "confidence" in text and "page" in text


# ---------------------------------------------------------------- plain messages for the UI (technical detail stays in str(error))
def req():
    return httpx2.Request("POST", "https://example.invalid")


@pytest.mark.parametrize("error, plain", [
    (anthropic.APITimeoutError(request=req()), "The AI service didn't respond."),
    (anthropic.APIConnectionError(request=req()), "The AI service didn't respond."),
    (anthropic.InternalServerError("overloaded", response=httpx2.Response(529, request=req()), body=None), "The AI service didn't respond."),
    (anthropic.RateLimitError("slow down", response=httpx2.Response(429, request=req()), body=None), "The AI service is busy right now (rate limit reached)."),
    (anthropic.AuthenticationError("bad key", response=httpx2.Response(401, request=req()), body=None), "The AI service rejected the request (API key or access problem)."),
])
def test_api_failures_carry_a_plain_user_message_and_keep_the_technical_detail(error, plain):
    llm, _ = client(error)
    with pytest.raises(LLMError) as e:
        llm.call("explain", [], Explanation)
    assert e.value.user_message == plain
    assert type(error).__name__ in str(e.value)                  # technical detail is still there for logs


def test_unusable_output_and_refusal_have_their_own_plain_messages(monkeypatch):
    llm, _ = client(validation_error(), validation_error())
    with pytest.raises(LLMError) as e:
        llm.call("explain", [], Explanation)
    assert e.value.user_message == "The AI's answer couldn't be used."
    llm, _ = client(reply(parsed=None, stop_reason="refusal"))
    with pytest.raises(LLMError) as e:
        llm.call("explain", [], Explanation)
    assert e.value.user_message == "The AI declined to read this document."
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError) as e:
        LLMClient(Settings()).call("explain", [], Explanation)
    assert "no API key" in e.value.user_message
