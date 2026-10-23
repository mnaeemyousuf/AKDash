"""AI calls (any OpenAI-compatible provider incl. open-source models, or Claude: spec generation, query fixing,
insights, chat) and the pipeline that checks every AI query by running it."""
from __future__ import annotations

import copy
import json
import re
from typing import Callable

import pandas as pd

import engine
import prompts

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MISTRAL_MODEL = "ministral-14b-latest"    # strongest model Mistral's free tier can call (30 requests/min)
CHAT_HISTORY_MESSAGES = 10
CHAT_SYSTEM = prompts.CHAT_SYSTEM.format(spec_rules=prompts.SPEC_SYSTEM)
# models that accept the server-side refusal fallback ("default" routing) on the Claude API
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_FIX_RETRIES = 2
RESULT_ROWS_FOR_AI = 30


class AIError(Exception):
    """A user-friendly AI failure message."""


class ClaudeLLM:
    def __init__(self, api_key: str | None, model: str = DEFAULT_MODEL, effort: str = "medium"):
        if not api_key:
            raise AIError("No Anthropic API key found. Add ANTHROPIC_API_KEY to .streamlit/secrets.toml.")
        import anthropic  # imported lazily: the SDK is slow to import and only needed for AI calls
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.effort = effort

    def json(self, system: str, user: str | None, schema: dict, max_tokens: int = 32000,
             messages: list[dict] | None = None) -> dict:
        """One structured-output call. Pass `user` for a single message, or `messages` for a conversation."""
        import anthropic
        params = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            messages=messages if messages is not None else [{"role": "user", "content": user}],
            output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            cache_control={"type": "ephemeral"},  # caches the long profile/spec prefix across chat turns
        )
        try:
            if self.model in FALLBACK_MODELS:
                with self.client.beta.messages.stream(
                    **params, betas=["server-side-fallback-2026-07-01"], fallbacks="default"
                ) as stream:
                    msg = stream.get_final_message()
            else:
                with self.client.messages.stream(**params) as stream:
                    msg = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise AIError("The Anthropic API key is invalid. Check .streamlit/secrets.toml.")
        except anthropic.PermissionDeniedError:
            raise AIError("This API key does not have access to the configured model.")
        except anthropic.NotFoundError:
            raise AIError(f"Model '{self.model}' was not found. Check the model name in secrets.toml.")
        except anthropic.RateLimitError:
            raise AIError("The AI service is rate-limited right now. Please wait a minute and try again.")
        except anthropic.BadRequestError as e:
            raise AIError(f"The AI request was rejected: {e.message}")
        except anthropic.APIStatusError as e:
            raise AIError(f"The AI service returned an error ({e.status_code}). Please try again.")
        except anthropic.APIConnectionError:
            raise AIError("Could not reach the AI service. Check your internet connection.")

        if msg.stop_reason == "refusal":
            raise AIError("The AI declined this request. Try rephrasing the instructions.")
        if msg.stop_reason == "max_tokens":
            raise AIError("The AI response was too long. Try asking for fewer widgets.")
        text = next((b.text for b in msg.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise AIError("The AI returned an invalid response. Please try again.")


# OpenAI-compatible chat-completions providers. Open-source models are available through all of these except
# Mistral's own API; "ollama" runs an open model on your own computer or server (free and private).
# "models": free-tier models in order of preference. If a model isn't available on the key's plan
# (not found / 0 requests allowed), the next one is tried automatically.
PROVIDERS = {
    # verified on a free key (Oct 2026): mistral-large/medium/small allow 0 requests on the free plan
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1", "key": "MISTRAL_API_KEY",
                "models": [DEFAULT_MISTRAL_MODEL, "ministral-8b-latest", "open-mistral-nemo"], "min_interval": 1.2},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1", "key": "OPENROUTER_API_KEY",
                   "models": ["openai/gpt-oss-120b:free", "qwen/qwen3-coder:free"], "min_interval": 3.1},
    # Groq's free tier allows ~8,000 tokens per minute, so one request (prompt + answer) must fit in that
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY",
             "models": ["openai/gpt-oss-120b", "openai/gpt-oss-20b"], "min_interval": 2.1,
             "request_tokens": 8000},
    # Google AI Studio's free tier serves the open Gemma 4 models
    "gemini": {"label": "Google AI Studio", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "key": "GEMINI_API_KEY", "models": ["gemma-4-31b-it", "gemma-4-26b-a4b-it"],
               "min_interval": 2.1},
    "ollama": {"label": "Ollama", "base_url": "http://localhost:11434/v1", "key": None,
               "models": ["qwen3:14b"], "min_interval": 0.0},
    "openai_compatible": {"label": "OpenAI-compatible API", "base_url": None, "key": "LLM_API_KEY",
                          "models": [], "min_interval": 0.0},
}
_LAST_CALL: dict[str, float] = {}
# Backup order when fallback_providers isn't set (only providers with an API key are used):
# Mistral (JSON-schema support, proven on AKDash) -> Gemma on Google AI Studio -> Groq (strongest model, but its
# free tier only fits small requests such as chat answers and query fixes) -> OpenRouter.
DEFAULT_FALLBACK_ORDER = ["mistral", "gemini", "groq", "openrouter"]


class OpenAICompatLLM:
    """Any OpenAI-compatible /chat/completions API (Mistral, OpenRouter, Groq, Google, Ollama, vLLM, ...)
    over plain HTTPS, with the same .json() interface as ClaudeLLM."""

    def __init__(self, provider: str, api_key: str | None, model: str | None = None, base_url: str | None = None,
                 temperature: float = 0.2):
        preset = PROVIDERS.get(provider, PROVIDERS["openai_compatible"])
        self.provider, self.label = provider, preset["label"]
        self.base_url = (base_url or preset["base_url"] or "").rstrip("/")
        # a model set in secrets.toml is tried first, then the provider's other free models
        self.models = list(dict.fromkeys(([model] if model else []) + preset["models"]))
        self.model = self.models[0] if self.models else None
        if not self.base_url:
            raise AIError("No AI base_url set. Add base_url to .streamlit/secrets.toml.")
        if not self.model:
            raise AIError("No AI model set. Add model to .streamlit/secrets.toml.")
        if preset["key"] and not api_key:
            raise AIError(f"No {self.label} API key found. Add {preset['key']} to .streamlit/secrets.toml "
                          f"(or as an environment variable).")
        self.api_key = api_key
        self.temperature = temperature
        self.min_interval = preset["min_interval"]
        self.request_tokens = preset.get("request_tokens")     # free-tier cap on prompt + answer, if any
        self.max_attempts = 6   # retries on rate limits / server errors (fewer when a backup provider exists)
        self.timeout = 900 if provider == "ollama" else 300   # local models can be slow
        # response_format support differs per provider/model: json_schema -> json_object -> plain prompt
        self.format_mode = "json_schema"
        # some models (e.g. Gemma on Google's API) reject system messages: then instructions go in the user turn
        self.system_role = True

    @property
    def schema_mode(self) -> bool:   # kept for older callers/tests
        return self.format_mode == "json_schema"

    @staticmethod
    def _merge_roles(messages: list[dict]) -> list[dict]:
        """Join consecutive messages from the same role into one."""
        out: list[dict] = []
        for m in messages:
            if out and out[-1]["role"] == m["role"]:
                out[-1] = {"role": m["role"], "content": out[-1]["content"] + "\n\n" + m["content"]}
            else:
                out.append(dict(m))
        return out

    def _post(self, body: dict) -> dict:
        import time

        import requests
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.provider == "openrouter":
            headers["X-Title"] = "AKDash"
        attempts = self.max_attempts
        for attempt in range(attempts):
            # free tiers allow only a few requests per minute: keep calls to one provider spaced out
            wait = _LAST_CALL.get(self.provider, 0.0) + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            _LAST_CALL[self.provider] = time.monotonic()
            try:
                r = requests.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout,
                                  headers=headers)
            except requests.RequestException:
                if attempt == attempts - 1:
                    hint = (" Is Ollama running (ollama serve) and is the model pulled?"
                            if self.provider == "ollama" else " Check your internet connection.")
                    raise AIError(f"Could not reach the {self.label} API.{hint}")
                time.sleep(2 ** attempt)
                continue
            r_headers = getattr(r, "headers", {}) or {}
            if r.status_code == 429 and r_headers.get("x-ratelimit-limit-req-minute") == "0":
                # the plan allows 0 requests/minute for this model: retrying cannot help
                raise AIError(f"Model '{self.model}' is not included in your {self.label} plan "
                              f"(0 requests/minute allowed). Choose another model in .streamlit/secrets.toml.")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < attempts - 1:
                retry_after = r_headers.get("Retry-After") or r_headers.get("retry-after")
                delay = float(retry_after) if retry_after and retry_after.replace(".", "").isdigit() else 2 ** attempt
                time.sleep(min(delay, 30))
                continue
            if r.status_code in (401, 403) and "tier" not in r.text.lower():
                detail = _LLMHTTPError(r.status_code, r.text[:500]).message
                raise AIError(f"The {self.label} API key is invalid or not allowed ({r.status_code}): {detail}. "
                              "Check the key in .streamlit/secrets.toml.")
            if r.status_code == 429:
                raise AIError(f"{self.label}'s rate limit was reached (free tiers allow only a few requests per "
                              "minute/day). Please wait a minute and try again.")
            if r.status_code >= 400:
                raise _LLMHTTPError(r.status_code, r.text[:500])
            return r.json()
        raise AIError(f"The {self.label} API did not respond. Please try again.")

    def _response_format(self, schema: dict) -> dict | None:
        if self.format_mode == "json_schema":
            return {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
        if self.format_mode == "json_object":
            return {"type": "json_object"}
        return None   # the model can't constrain output: rely on the prompt + tolerant parsing

    # move on to the provider's next free model when a model isn't on the plan, or is overloaded right now
    MODEL_UNAVAILABLE = ("was not found", "not included in your", "not available on your",
                         "returned an error (5", "did not respond", "high demand")

    def json(self, system: str, user: str | None, schema: dict, max_tokens: int = 32000,
             messages: list[dict] | None = None) -> dict:
        """Ask the current model; if the key's plan doesn't offer it, move on to the next free model."""
        while True:
            try:
                return self._json_one_model(system, user, schema, max_tokens, messages)
            except AIError as e:
                if not any(s in str(e) for s in self.MODEL_UNAVAILABLE) or len(self.models) < 2:
                    raise
                self.models.pop(0)                 # remember for the next calls too
                self.model = self.models[0]
                self.format_mode, self.system_role = "json_schema", True

    def _json_one_model(self, system: str, user: str | None, schema: dict, max_tokens: int,
                        messages: list[dict] | None) -> dict:
        convo = self._merge_roles(messages if messages is not None else [{"role": "user", "content": user}])
        instructions = system + ("\n\nReturn ONLY a JSON object (no markdown, no explanation) that follows this "
                                 "JSON schema exactly:\n" + json.dumps(schema))
        if self.request_tokens:
            # rough size check (~3.5 characters per token) so a request that can't fit isn't sent at all
            prompt_tokens = int((len(instructions) + sum(len(m["content"]) for m in convo)) / 3.5)
            if prompt_tokens + 1500 > self.request_tokens:
                raise AIError(f"This request is too large for the {self.label} free tier "
                              f"(about {prompt_tokens:,} tokens; limit {self.request_tokens:,} per request).")
            max_tokens = min(max_tokens, self.request_tokens - prompt_tokens)
        for attempt in range(5):
            if self.system_role:
                msgs = [{"role": "system", "content": instructions}] + convo
            else:
                first = convo[0]
                msgs = ([{"role": "user", "content": f"INSTRUCTIONS:\n{instructions}\n\n{first['content']}"}]
                        + convo[1:] if first["role"] == "user"
                        else [{"role": "user", "content": f"INSTRUCTIONS:\n{instructions}"}] + convo)
            body = {"model": self.model, "temperature": self.temperature, "max_tokens": max_tokens, "messages": msgs}
            fmt = self._response_format(schema)
            if fmt:
                body["response_format"] = fmt
            try:
                data = self._post(body)
            except _LLMHTTPError as e:
                low = e.text.lower()
                if (e.status in (400, 422) and self.system_role
                        and ("instruction is not enabled" in low or "system role" in low
                             or ("system" in low and "not supported" in low))):
                    self.system_role = False   # e.g. Gemma: put the instructions in the user message instead
                    continue
                if e.status in (400, 422) and self.format_mode != "none" and not _is_model_error(e.text):
                    # this provider/model rejected the output format: try a simpler one
                    self.format_mode = "json_object" if self.format_mode == "json_schema" else "none"
                    continue
                if e.status == 403 and "tier" in e.text.lower():
                    default = (PROVIDERS.get(self.provider, {}).get("models") or [None])[0]
                    raise AIError(f"Model '{self.model}' is not available on your {self.label} plan. "
                                  + (f'Set model = "{default}" ' if default else "Choose another model ")
                                  + "in .streamlit/secrets.toml and restart the app.")
                if e.status == 404 or (e.status == 400 and _is_model_error(e.text)):
                    raise AIError(f"{self.label} model '{self.model}' was not found. Check `model` in secrets.toml.")
                raise AIError(f"The {self.label} API returned an error ({e.status}): {e.message}")
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise AIError("The AI response was too long. Try asking for fewer widgets.")
            content = (choice.get("message") or {}).get("content") or ""
            if isinstance(content, list):   # some models return content chunks
                content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
            parsed = _parse_json(content)
            if parsed is not None:
                return parsed
        raise AIError("The AI returned an invalid response. Please try again.")


class MistralLLM(OpenAICompatLLM):
    """Mistral, kept as a named client (same OpenAI-style API as the other providers)."""

    def __init__(self, api_key: str | None, model: str = DEFAULT_MISTRAL_MODEL, temperature: float = 0.2):
        super().__init__("mistral", api_key, model, temperature=temperature)


def _is_model_error(text: str) -> bool:
    t = text.lower()
    return "model" in t and any(w in t for w in ("not found", "does not exist", "invalid model", "unknown model",
                                                  "no endpoints", "decommissioned"))


class _LLMHTTPError(Exception):
    def __init__(self, status: int, text: str):
        super().__init__(f"{status}: {text}")
        self.status, self.text = status, text
        try:
            body = json.loads(text)
            err = body.get("error")
            self.message = (err.get("message") if isinstance(err, dict) else err) or body.get("message") or text
        except (json.JSONDecodeError, AttributeError):
            self.message = text


def _parse_json(text: str) -> dict | None:
    """Parse a JSON object, tolerating ```json fences or text around it."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def provider_key_name(provider: str) -> str | None:
    """Name of the secret / environment variable that holds this provider's API key."""
    if provider == "anthropic":
        return "ANTHROPIC_API_KEY"
    return PROVIDERS.get(provider, PROVIDERS["openai_compatible"])["key"]


def make_llm(provider: str, api_key: str | None, model: str | None = None, base_url: str | None = None):
    """Create the configured AI client: 'anthropic', or any OpenAI-compatible provider in PROVIDERS."""
    if provider == "anthropic":
        return ClaudeLLM(api_key, model or DEFAULT_MODEL)
    if provider == "mistral" and not base_url:
        return MistralLLM(api_key, model or DEFAULT_MISTRAL_MODEL)
    if provider in PROVIDERS:
        return OpenAICompatLLM(provider, api_key, model, base_url)
    raise AIError(f"Unknown AI provider '{provider}'. Use one of: anthropic, {', '.join(PROVIDERS)}.")


class FallbackLLM:
    """Tries several providers in order: when one is rate-limited or unreachable, the next one answers.
    Lets small free tiers (e.g. Groq + Google + OpenRouter) add up to more daily capacity."""

    SWITCH_ON = ("rate limit", "did not respond", "could not reach", "not included in your", "not available on your",
                 "returned an error (5", "returned an error (413", "too large", "too long", "context length",
                 "insufficient credits", "quota", "invalid response")

    def __init__(self, clients: list):
        if not clients:
            raise AIError("No AI provider is configured. Add an API key to .streamlit/secrets.toml.")
        self.clients = clients
        for c in clients[:-1]:   # hand over to the backup after ~7s of retries instead of ~30s
            if hasattr(c, "max_attempts"):
                c.max_attempts = 3
        self.used: list[str] = []   # labels of the providers that answered (shown to the user)

    def __getattr__(self, name):
        return getattr(self.clients[0], name)

    def json(self, system: str, user: str | None, schema: dict, max_tokens: int = 32000,
             messages: list[dict] | None = None) -> dict:
        errors = []
        for client in self.clients:
            try:
                out = client.json(system, user, schema, max_tokens=max_tokens, messages=messages)
                self.used.append(getattr(client, "label", "AI"))
                return out
            except AIError as e:
                errors.append(str(e))
                if not any(s in str(e).lower() for s in self.SWITCH_ON):
                    raise   # a real error (bad key, bad request): don't hide it behind another provider
        raise AIError("All AI providers are busy right now. " + " | ".join(errors))


class PersonaLLM:
    """Wraps any client so every prompt also tells the AI who the user is (their profile persona)."""

    def __init__(self, inner, persona: str):
        self.inner, self.persona = inner, (persona or "").strip()

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def json(self, system: str, user: str | None, schema: dict, max_tokens: int = 32000,
             messages: list[dict] | None = None) -> dict:
        if self.persona:
            system = (f"{system}\n\nABOUT THE USER (tailor focus, wording, language and level of detail to them; "
                      f"never invent numbers because of it):\n{self.persona}")
        return self.inner.json(system, user, schema, max_tokens=max_tokens, messages=messages)


# ---------- spec helpers ----------

def _snake(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_") or "widget"


def normalize_spec(spec: dict, tables: dict[str, pd.DataFrame]) -> dict:
    """Make the spec safe to render: unique ids, known tabs/types, valid filters."""
    spec = copy.deepcopy(spec)
    seen = set()
    widgets = []
    for w in spec.get("widgets", []):
        if w.get("type") not in prompts.WIDGET_TYPES:
            continue
        if w.get("tab") not in prompts.TABS:
            w["tab"] = "Overview" if w["type"] == "kpi" else "Breakdown"
        wid = _snake(w.get("id") or w.get("title"))
        base, i = wid, 2
        while wid in seen:
            wid, i = f"{base}_{i}", i + 1
        seen.add(wid)
        w["id"] = wid
        w.setdefault("y", [])
        w.setdefault("format", "number")
        w.setdefault("higher_is_better", True)
        widgets.append(w)
    spec["widgets"] = widgets

    all_columns = {c for df in tables.values() for c in df.columns}
    spec["filters"] = [f for f in spec.get("filters", []) if f.get("column") in all_columns][:3]
    dc = spec.get("date_column")
    if dc and not (dc.get("table") in tables and dc.get("column") in tables[dc["table"]].columns
                   and pd.api.types.is_datetime64_any_dtype(tables[dc["table"]][dc["column"]])):
        spec["date_column"] = _guess_date_column(tables)
    return spec


def _guess_date_column(tables: dict[str, pd.DataFrame]) -> dict | None:
    for name, df in tables.items():
        for c in df.columns:
            if pd.api.types.is_datetime64_any_dtype(df[c]):
                return {"table": name, "column": c}
    return None


def check_result(widget: dict, df: pd.DataFrame) -> str | None:
    """Return an error message if the result does not fit the widget, else None."""
    t = widget["type"]
    cols = set(df.columns)
    if t == "kpi":
        if "value" not in cols:
            return f"KPI query must return a column named 'value'; it returned {list(df.columns)}."
        if len(df) != 1:
            return f"KPI query must return exactly 1 row; it returned {len(df)}."
        if df["value"].notna().all() and not pd.api.types.is_numeric_dtype(df["value"]):
            return "KPI 'value' must be numeric."
        return None
    if t == "table":
        return None
    needed = [widget.get("x")] + list(widget.get("y") or [])
    if widget.get("color"):
        needed.append(widget["color"])
    if t == "heatmap":
        needed.append(widget.get("value"))
    missing = [c for c in needed if c and c not in cols]
    if not widget.get("x") or not widget.get("y"):
        return f"{t} widget needs x and at least one y column."
    if missing:
        return f"Result columns {list(df.columns)} do not include {missing} used in x/y/color/value."
    return None


def run_widget(con, widget: dict, max_rows: int = engine.MAX_RESULT_ROWS) -> tuple[pd.DataFrame | None, str | None]:
    try:
        df = engine.run_query(con, widget["sql"], max_rows)
    except engine.QueryError as e:
        return None, str(e)
    err = check_result(widget, df)
    return (None, err) if err else (df, None)


def fix_widget(llm, profile: str, widget: dict, error: str) -> dict:
    payload = {k: v for k, v in widget.items() if k != "error"}
    fixed = llm.json(prompts.FIX_SYSTEM,
                     prompts.FIX_USER.format(profile=profile, widget=json.dumps(payload), error=error),
                     prompts.WIDGET_SCHEMA, max_tokens=8000)
    fixed["id"] = widget["id"]
    return fixed


def resolve_widgets(llm, spec: dict, tables: dict[str, pd.DataFrame], profile: str,
                    progress: Callable[[str], None] | None = None) -> tuple[dict, dict[str, pd.DataFrame]]:
    """Run every widget query; ask the AI to fix failures (max 2 retries).
    Returns (spec with fixed SQL and 'error' on widgets that still fail, results by widget id)."""
    spec = copy.deepcopy(spec)
    results: dict[str, pd.DataFrame] = {}
    con = engine.connect(tables)
    try:
        for i, w in enumerate(spec["widgets"]):
            if progress:
                progress(f"Running query {i + 1}/{len(spec['widgets'])}: {w['title']}")
            w.pop("error", None)
            df, err = run_widget(con, w)
            attempts = 0
            while err and llm is not None and attempts < MAX_FIX_RETRIES:
                attempts += 1
                if progress:
                    progress(f"Fixing '{w['title']}' (attempt {attempts})")
                try:
                    candidate = fix_widget(llm, profile, w, err)
                except AIError as e:
                    err = f"{err} (AI fix failed: {e})"
                    break
                if candidate.get("type") not in prompts.WIDGET_TYPES:
                    candidate["type"] = w["type"]
                if candidate.get("tab") not in prompts.TABS:
                    candidate["tab"] = w["tab"]
                df, new_err = run_widget(con, candidate)
                w.clear()
                w.update(candidate)
                err = new_err
            if err:
                w["error"] = err
            else:
                results[w["id"]] = df
    finally:
        con.close()
    return spec, results


def run_spec(spec: dict, tables: dict[str, pd.DataFrame]) -> dict[str, tuple[pd.DataFrame | None, str | None]]:
    """Run all widgets without AI (used for rendering with filters and for refreshes)."""
    out = {}
    con = engine.connect(tables)
    try:
        for w in spec.get("widgets", []):
            out[w["id"]] = run_widget(con, w)
    finally:
        con.close()
    return out


def kpi_changes(spec: dict, current: dict[str, tuple], previous_tables: dict[str, pd.DataFrame] | None) -> list[dict]:
    """KPI value now vs the same KPI SQL run on the previous upload's data."""
    if not previous_tables:
        return []
    prev = run_spec({"widgets": [w for w in spec["widgets"] if w["type"] == "kpi"]}, previous_tables)
    changes = []
    for w in spec["widgets"]:
        if w["type"] != "kpi":
            continue
        now_df, _ = current.get(w["id"], (None, None))
        prev_df, _ = prev.get(w["id"], (None, None))
        if now_df is None or prev_df is None:
            continue
        now, before = now_df["value"].iloc[0], prev_df["value"].iloc[0]
        if pd.isna(now) or pd.isna(before):
            continue
        now, before = float(now), float(before)
        changes.append({"id": w["id"], "title": w["title"], "now": now, "previous": before,
                        "change": now - before,
                        "change_pct": round((now - before) / abs(before) * 100, 2) if before else None})
    return changes


# ---------- AI steps ----------

def generate_spec(llm, profile: str, instructions: str, tables: dict[str, pd.DataFrame]) -> dict:
    raw = llm.json(prompts.SPEC_SYSTEM,
                   prompts.SPEC_USER.format(profile=profile, instructions=instructions.strip() or
                                            "(no specific instructions: build a general overview dashboard)"),
                   prompts.SPEC_SCHEMA)
    spec = normalize_spec(raw, tables)
    if not spec["widgets"]:
        raise AIError("The AI did not return any widgets. Try more specific instructions.")
    return expand_if_thin(llm, spec, profile, instructions, tables)


# Minimum detail every dashboard must reach (the AI is asked once to fill any gap).
MIN_WIDGETS = 20
MIN_PER_TAB = {"Overview": 6, "Trends": 4, "Breakdown": 6, "Exceptions": 3}   # Overview counts KPI cards


def detail_gaps(spec: dict, tables: dict[str, pd.DataFrame]) -> list[str]:
    widgets = spec["widgets"]
    has_dates = any(pd.api.types.is_datetime64_any_dtype(df[c]) for df in tables.values() for c in df.columns)
    gaps = []
    kpis = sum(w["type"] == "kpi" for w in widgets)
    if kpis < MIN_PER_TAB["Overview"]:
        gaps.append(f"Overview needs {MIN_PER_TAB['Overview'] - kpis} more KPI cards.")
    for tab in ("Trends", "Breakdown", "Exceptions"):
        if tab == "Trends" and not has_dates:
            continue
        n = sum(w["tab"] == tab for w in widgets)
        if n < MIN_PER_TAB[tab]:
            gaps.append(f"{tab} needs {MIN_PER_TAB[tab] - n} more widgets.")
    if len(widgets) < MIN_WIDGETS:
        gaps.append(f"The dashboard needs at least {MIN_WIDGETS - len(widgets)} more widgets in total.")
    return gaps


def expand_if_thin(llm, spec: dict, profile: str, instructions: str, tables: dict[str, pd.DataFrame]) -> dict:
    gaps = detail_gaps(spec, tables)
    if not gaps:
        return spec
    existing = "\n".join(f"{w['id']} | {w['tab']} | {w['type']} | {w['title']}" for w in spec["widgets"])
    try:
        out = llm.json(prompts.EXPAND_SYSTEM + prompts.SPEC_SYSTEM,
                       prompts.EXPAND_USER.format(profile=profile, instructions=instructions.strip() or "(none)",
                                                  existing=existing, missing="\n".join(gaps)),
                       prompts.EXPAND_SCHEMA)
    except AIError:
        return spec   # the dashboard is still usable without the extra widgets
    merged = dict(spec, widgets=spec["widgets"] + list(out.get("widgets") or []))
    return normalize_spec(merged, tables)


def _records(df: pd.DataFrame, n: int) -> list[dict]:
    head = df.head(n).copy()
    for c in head.columns:
        if pd.api.types.is_datetime64_any_dtype(head[c]):
            head[c] = head[c].dt.strftime("%Y-%m-%d")
        elif pd.api.types.is_float_dtype(head[c]):
            head[c] = head[c].round(2)
    return json.loads(head.to_json(orient="records", date_format="iso"))


def results_for_ai(spec: dict, results: dict[str, pd.DataFrame]) -> list[dict]:
    out = []
    for w in spec["widgets"]:
        df = results.get(w["id"])
        if df is None:
            continue
        item = {"title": w["title"], "tab": w["tab"], "type": w["type"], "description": w.get("description", "")}
        if w["type"] == "kpi":
            v = df["value"].iloc[0]
            item["value"] = None if pd.isna(v) else round(float(v), 2)
            item["format"], item["unit"] = w.get("format"), w.get("unit")
        else:
            item["rows_total"] = int(len(df))
            item["data"] = _records(df, RESULT_ROWS_FOR_AI)
        out.append(item)
    return out


def generate_insights(llm, instructions: str, spec: dict, results: dict[str, pd.DataFrame],
                      changes: list[dict] | dict) -> dict:
    payload = json.dumps(results_for_ai(spec, results), ensure_ascii=False, default=str)
    return llm.json(prompts.INSIGHTS_SYSTEM,
                    prompts.INSIGHTS_USER.format(instructions=instructions.strip() or "(none)",
                                                 results=payload,
                                                 changes=json.dumps(changes, default=str)),
                    prompts.INSIGHTS_SCHEMA, max_tokens=16000)


# ---------- PDF reports ----------

PDF_TEXT_LIMIT = 30_000


def extract_tables_from_text(llm, name: str, text: str) -> list[tuple[str, list[list]]]:
    """For PDFs with text but no table structure: the AI copies the figures into tables (header row + rows).
    The app then cleans and calculates on them like any sheet."""
    out = llm.json(prompts.PDF_EXTRACT_SYSTEM, prompts.PDF_EXTRACT_USER.format(name=name, text=text[:PDF_TEXT_LIMIT]),
                   prompts.PDF_EXTRACT_SCHEMA, max_tokens=16000)
    tables = []
    for i, t in enumerate(out.get("tables") or []):
        columns = [str(c) for c in t.get("columns") or []]
        rows = [list(r)[:len(columns)] + [None] * (len(columns) - len(r)) for r in t.get("rows") or [] if r]
        if columns and rows:
            tables.append((_snake(t.get("name") or f"table_{i + 1}"), [columns] + rows))
    return tables


# ---------- daily refresh ----------

def _mentions(sql: str, name: str) -> bool:
    return re.search(rf'(?<![\w."]){re.escape(name)}(?![\w"])|"{re.escape(name)}"', sql, re.IGNORECASE) is not None


def affected_widgets(spec: dict | None, report: dict) -> dict[str, list[str]]:
    """Map each missing table / table.column (from ingest.compare_schemas) to the widget titles that use it."""
    out: dict[str, list[str]] = {}
    widgets = (spec or {}).get("widgets", [])
    for table in report["missing_tables"]:
        out[table] = [w["title"] for w in widgets if _mentions(w["sql"], table)]
    for table, cols in report["missing_columns"].items():
        for col in cols:
            out[f"{table}.{col}"] = [w["title"] for w in widgets
                                     if _mentions(w["sql"], table) and _mentions(w["sql"], col)]
    return out


def refresh_insights(llm, spec: dict, instructions: str, tables: dict[str, pd.DataFrame],
                     previous_tables: dict[str, pd.DataFrame] | None, upload_date: str) -> dict:
    """After a new upload: run the saved spec on the new data (no redesign) and rewrite only the insights,
    including what changed since the previous upload. Returns the spec with new insights."""
    current = run_spec(spec, tables)
    results = {wid: df for wid, (df, err) in current.items() if df is not None}
    changes = {
        "upload_date": upload_date,
        "kpis": kpi_changes(spec, current, previous_tables),
        "row_counts": {name: {"previous": int(len(previous_tables[name])) if previous_tables and
                              name in previous_tables else None, "now": int(len(df))}
                       for name, df in tables.items()},
    }
    new_spec = copy.deepcopy(spec)
    new_spec["insights"] = generate_insights(llm, instructions, spec, results, changes)
    return new_spec


# ---------- chat ----------

def spec_for_ai(spec: dict) -> dict:
    """The spec without insights and internal error fields (keeps the chat prompt small)."""
    return {"title": spec.get("title", ""), "date_column": spec.get("date_column"),
            "filters": spec.get("filters", []),
            "widgets": [{k: v for k, v in w.items() if k != "error"} for w in spec.get("widgets", [])]}


def chat_messages(context: str, history: list[dict], message: str) -> list[dict]:
    """Context first, then only the last 10 chat messages, then the new message."""
    recent = [{"role": m["role"], "content": m["content"]} for m in history[-CHAT_HISTORY_MESSAGES:]
              if m["role"] in ("user", "assistant") and m["content"].strip()]
    return [{"role": "user", "content": context}] + recent + [{"role": "user", "content": message}]


def answer_question(llm, profile: str, tables: dict[str, pd.DataFrame], question: str, sql: str,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Run the AI's SQL (fixing it up to 2 times), then have the AI explain the result."""
    error = None
    for attempt in range(MAX_FIX_RETRIES + 1):
        try:
            df = engine.run_on_tables(tables, sql, max_rows=1000)
            error = None
            break
        except engine.QueryError as e:
            error = str(e)
            if attempt == MAX_FIX_RETRIES:
                break
            if progress:
                progress(f"Fixing the query (attempt {attempt + 1})")
            sql = llm.json(prompts.SQL_FIX_SYSTEM,
                           prompts.SQL_FIX_USER.format(profile=profile, question=question, sql=sql, error=error),
                           prompts.SQL_FIX_SCHEMA, max_tokens=4000)["sql"]
    if error:
        return {"reply": f"Sorry, I couldn't calculate that. The query failed: {error}", "sql": sql, "data": None}
    if progress:
        progress("Explaining the result…")
    shown = _records(df, RESULT_ROWS_FOR_AI * 2)
    answer = llm.json(prompts.ANSWER_SYSTEM,
                      prompts.ANSWER_USER.format(question=question, sql=sql, rows=len(df), shown=len(shown),
                                                 result=json.dumps(shown, ensure_ascii=False, default=str)),
                      prompts.ANSWER_SCHEMA, max_tokens=4000)["answer"]
    return {"reply": answer, "sql": sql, "data": df}


def chat_turn(llm, message: str, spec: dict, instructions: str, history: list[dict],
              tables: dict[str, pd.DataFrame], profile: str,
              progress: Callable[[str], None] | None = None) -> dict:
    """One chat turn. Returns {"kind": "update_spec"|"query"|"answer", "reply", "spec", "sql", "data"}."""
    context = prompts.CHAT_CONTEXT.format(profile=profile, instructions=instructions.strip() or "(none)",
                                          spec=json.dumps(spec_for_ai(spec), ensure_ascii=False))
    if progress:
        progress("Thinking…")
    out = llm.json(CHAT_SYSTEM, None, prompts.CHAT_SCHEMA, messages=chat_messages(context, history, message))
    action = out.get("action")

    if action == "update_spec" and out.get("spec"):
        new_spec = normalize_spec(out["spec"], tables)
        if not new_spec["widgets"]:
            return {"kind": "answer", "reply": "That change would remove every widget, so I left the dashboard "
                                               "as it is.", "spec": None, "sql": None, "data": None}
        new_spec, _ = resolve_widgets(llm, new_spec, tables, profile, progress)
        new_spec["insights"] = spec.get("insights")
        reply = out.get("reply") or "Dashboard updated."
        failed = [w["title"] for w in new_spec["widgets"] if w.get("error")]
        if failed:
            reply += f"\n\n⚠️ Could not calculate: {', '.join(failed)}."
        return {"kind": "update_spec", "reply": reply, "spec": new_spec, "sql": None, "data": None}

    if action == "query" and out.get("sql"):
        result = answer_question(llm, profile, tables, message, out["sql"], progress)
        return {"kind": "query", "spec": None, **result}

    return {"kind": "answer", "reply": out.get("reply") or "Sorry, I didn't understand that.",
            "spec": None, "sql": None, "data": None}


def build_dashboard(llm, tables: dict[str, pd.DataFrame], instructions: str, profile: str,
                    previous_tables: dict[str, pd.DataFrame] | None = None,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Full pipeline: spec -> run/fix queries -> insights. Returns the final spec (with insights)."""
    if progress:
        progress("Designing the dashboard…")
    spec = generate_spec(llm, profile, instructions, tables)
    spec, results = resolve_widgets(llm, spec, tables, profile, progress)
    if not results:
        raise AIError("None of the dashboard queries could be run. Please try again.")
    # widgets that still fail after the fixes are left out of a new dashboard (listed for the user instead)
    spec["skipped_widgets"] = [w["title"] for w in spec["widgets"] if w.get("error")]
    spec["widgets"] = [w for w in spec["widgets"] if not w.get("error")]
    if progress:
        progress("Writing insights from the computed results…")
    current = {wid: (df, None) for wid, df in results.items()}
    spec["insights"] = generate_insights(llm, instructions, spec, results,
                                         kpi_changes(spec, current, previous_tables))
    return spec
