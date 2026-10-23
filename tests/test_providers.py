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
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "gemma-4-31b-it"),
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


def test_unknown_model_is_reported_after_all_free_models(http):
    calls, queue = http
    queue += [FakeResponse(404, {"error": {"message": "model not found"}})] * 4   # "nope" + 3 free models
    with pytest.raises(ai.AIError, match="was not found"):
        ai.make_llm("groq", "k", "nope").json("s", "u", {})
    assert len(calls) == 4


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


def test_groq_skips_requests_too_large_for_free_tier(http):
    calls, queue = http
    llm = ai.make_llm("groq", "k")
    with pytest.raises(ai.AIError, match="too large for the Groq free tier"):
        llm.json("x" * 40_000, "u", {})
    assert calls == []                                           # never sent
    queue.append(ok('{"a": 1}'))
    llm.json("short system", "u", {}, max_tokens=32000)
    assert calls[0]["body"]["max_tokens"] < 8000                 # answer budget fits the free-tier cap


def test_chain_mistral_gemini_groq_with_big_request(http):
    """Mistral rate-limited -> Gemini answers; Groq is never tried for a big request."""
    calls, queue = http
    queue += [FakeResponse(429, {"message": "rate limit"})] * 6 + [ok('{"by": "gemini"}')]
    llm = ai.FallbackLLM([ai.make_llm("mistral", "k"), ai.make_llm("gemini", "k"), ai.make_llm("groq", "k")])
    assert llm.json("x" * 40_000, "u", {}) == {"by": "gemini"}
    assert llm.used == ["Google AI Studio"]
    assert not any("groq" in c["url"] for c in calls)


def test_chain_reaches_groq_for_small_request(http):
    calls, queue = http
    queue += ([FakeResponse(429, {"message": "rate limit"})] * 6
              + [FakeResponse(429, {"error": {"message": "quota exceeded"}})] * 6 + [ok('{"by": "groq"}')])
    llm = ai.FallbackLLM([ai.make_llm("mistral", "k"), ai.make_llm("gemini", "k"), ai.make_llm("groq", "k")])
    assert llm.json("small", "u", {}) == {"by": "groq"}
    assert llm.used == ["Groq"]
    assert "groq" in calls[-1]["url"]


def test_gemma_without_system_role(http):
    calls, queue = http
    queue += [FakeResponse(400, {"error": {"message": "Developer instruction is not enabled for models/gemma-3"}}),
              ok('{"ok": 1}')]
    llm = ai.make_llm("gemini", "k")
    assert llm.json("SYSTEM RULES", "the question", {}) == {"ok": 1}
    retry = calls[1]["body"]["messages"]
    assert [m["role"] for m in retry] == ["user"]
    assert retry[0]["content"].startswith("INSTRUCTIONS:\nSYSTEM RULES") and "the question" in retry[0]["content"]
    assert calls[1]["body"]["response_format"]["type"] == "json_schema"   # format kept; only the role changed


def test_free_model_auto_switch(http):
    """The first free model isn't on the key's plan -> the next one is used, and remembered."""
    calls, queue = http
    queue += [FakeResponse(404, {"error": {"message": "models/gemma-4-31b-it is not found"}}),
              ok('{"ok": 1}'), ok('{"ok": 2}')]
    llm = ai.make_llm("gemini", "k")
    assert llm.json("s", "u", {}) == {"ok": 1}
    assert [c["body"]["model"] for c in calls] == ["gemma-4-31b-it", "gemma-4-26b-a4b-it"]
    llm.json("s", "u", {})
    assert calls[-1]["body"]["model"] == "gemma-4-26b-a4b-it"


def test_mistral_zero_quota_model_switches(http):
    calls, queue = http
    queue += [FakeResponse(429, {"message": "Rate limit exceeded"}, {"x-ratelimit-limit-req-minute": "0"}),
              ok('{"ok": 1}')]
    llm = ai.make_llm("mistral", "k", "mistral-large-latest")
    assert llm.json("s", "u", {}) == {"ok": 1}
    assert [c["body"]["model"] for c in calls] == ["mistral-large-latest", "ministral-14b-latest"]


def test_model_from_secrets_tried_first(http):
    calls, queue = http
    queue.append(ok('{"ok": 1}'))
    ai.make_llm("groq", "k", "qwen/qwen3.6-27b").json("s", "u", {})
    assert calls[0]["body"]["model"] == "qwen/qwen3.6-27b"


def test_unknown_provider():
    with pytest.raises(ai.AIError, match="Unknown AI provider"):
        ai.make_llm("nope", "k")
