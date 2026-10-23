"""OpenAI-compatible providers (OpenRouter, Groq, Google, Ollama, custom) with the HTTP layer mocked."""
import json

import pytest

import ai


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


def ok(content):
    return FakeResponse(200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


@pytest.fixture
def http(monkeypatch):
    import requests
    calls, queue = [], []

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append({"url": url, "body": json, "headers": headers})
        return queue.pop(0)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr("time.sleep", lambda s: None)
    return calls, queue


@pytest.mark.parametrize("provider,url,model", [
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions", "openai/gpt-oss-120b:free"),
    ("groq", "https://api.groq.com/openai/v1/chat/completions", "openai/gpt-oss-120b"),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemma-3-27b-it"),
    ("mistral", "https://api.mistral.ai/v1/chat/completions", "ministral-14b-latest"),
])
def test_presets(http, provider, url, model):
    calls, queue = http
    queue.append(ok('{"a": 1}'))
    assert ai.make_llm(provider, "key-123").json("sys", "u", {"type": "object"}) == {"a": 1}
    assert calls[0]["url"] == url
    assert calls[0]["body"]["model"] == model
    assert calls[0]["headers"]["Authorization"] == "Bearer key-123"


def test_key_names():
    assert ai.provider_key_name("openrouter") == "OPENROUTER_API_KEY"
    assert ai.provider_key_name("groq") == "GROQ_API_KEY"
    assert ai.provider_key_name("gemini") == "GEMINI_API_KEY"
    assert ai.provider_key_name("ollama") is None
    assert ai.provider_key_name("anthropic") == "ANTHROPIC_API_KEY"


def test_missing_key_message():
    with pytest.raises(ai.AIError, match="OPENROUTER_API_KEY"):
        ai.make_llm("openrouter", None)


def test_ollama_needs_no_key_and_custom_model(http):
    calls, queue = http
    queue.append(ok('{"ok": true}'))
    llm = ai.make_llm("ollama", None, "gpt-oss:20b")
    assert llm.json("s", "u", {}) == {"ok": True}
    assert calls[0]["url"] == "http://localhost:11434/v1/chat/completions"
    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["body"]["model"] == "gpt-oss:20b"


def test_custom_openai_compatible_endpoint(http):
    calls, queue = http
    queue.append(ok('{"ok": 1}'))
    llm = ai.make_llm("openai_compatible", "k", "my-model", "https://llm.example.com/v1/")
    llm.json("s", "u", {})
    assert calls[0]["url"] == "https://llm.example.com/v1/chat/completions"
    with pytest.raises(ai.AIError, match="base_url"):
        ai.make_llm("openai_compatible", "k", "m")


def test_format_fallback_chain_to_plain_prompt(http):
    calls, queue = http
    queue += [FakeResponse(400, {"error": {"message": "response_format json_schema unsupported"}}),
              FakeResponse(400, {"error": {"message": "json mode is not enabled for this model"}}),
              ok('Here you go:\n```json\n{"x": 2}\n```')]
    llm = ai.make_llm("gemini", "k")
    assert llm.json("s", "u", {"type": "object"}) == {"x": 2}
    assert calls[0]["body"]["response_format"]["type"] == "json_schema"
    assert calls[1]["body"]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[2]["body"]
    assert "JSON schema" in calls[2]["body"]["messages"][0]["content"]   # schema still in the prompt


def test_unknown_model_is_reported(http):
    calls, queue = http
    queue.append(FakeResponse(404, {"error": {"message": "model not found"}}))
    with pytest.raises(ai.AIError, match="was not found"):
        ai.make_llm("groq", "k", "nope").json("s", "u", {})


def test_openrouter_error_message_is_shown(http):
    calls, queue = http
    queue.append(FakeResponse(402, {"error": {"message": "Insufficient credits"}}))
    with pytest.raises(ai.AIError, match="Insufficient credits"):
        ai.make_llm("openrouter", "k").json("s", "u", {})


def test_fallback_switches_on_rate_limit(http):
    calls, queue = http
    queue += [FakeResponse(429, {"error": {"message": "rate limit"}})] * 6 + [ok('{"from": "gemini"}')]
    llm = ai.FallbackLLM([ai.make_llm("groq", "k"), ai.make_llm("gemini", "k")])
    assert llm.json("s", "u", {}) == {"from": "gemini"}
    assert "groq" in calls[0]["url"] and "googleapis" in calls[-1]["url"]


def test_fallback_does_not_hide_real_errors(http):
    calls, queue = http
    queue.append(FakeResponse(401, {"error": {"message": "bad key"}}))
    llm = ai.FallbackLLM([ai.make_llm("groq", "k"), ai.make_llm("gemini", "k")])
    with pytest.raises(ai.AIError, match="bad key"):
        llm.json("s", "u", {})
    assert len(calls) == 1


def test_unknown_provider():
    with pytest.raises(ai.AIError, match="Unknown AI provider"):
        ai.make_llm("nope", "k")
