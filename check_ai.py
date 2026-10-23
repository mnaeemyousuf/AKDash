"""Check which AI models work with your keys (free tiers differ per account).

Run:  python check_ai.py
Reads .streamlit/secrets.toml (or environment variables), sends one tiny request per model, never prints keys.
"""
import os
import tomllib
from pathlib import Path

import ai

secrets_path = Path(__file__).parent / ".streamlit" / "secrets.toml"
secrets = tomllib.load(open(secrets_path, "rb")) if secrets_path.exists() else {}


def setting(name: str):
    return secrets.get(name) or os.environ.get(name)


SCHEMA = {"type": "object", "properties": {"answer": {"type": "number"}}, "required": ["answer"],
          "additionalProperties": False}

print(f"Main provider: {setting('provider') or 'mistral'}\n")
for name in ai.DEFAULT_FALLBACK_ORDER:
    key_name = ai.provider_key_name(name)
    key = setting(key_name)
    if not key:
        print(f"{ai.PROVIDERS[name]['label']:18} no {key_name} - skipped")
        continue
    for model in ai.PROVIDERS[name]["models"]:
        client = ai.OpenAICompatLLM(name, key, model)
        client.models = [model]   # test this model only, no auto-switch
        try:
            out = client.json("Answer with JSON only.", 'What is 12 * 7? Reply as {"answer": number}.', SCHEMA,
                              max_tokens=300)
            status = "OK" if out.get("answer") == 84 else f"answered {out}"
        except ai.AIError as e:
            status = f"not usable: {str(e)[:110]}"
        print(f"{ai.PROVIDERS[name]['label']:18} {model:28} {status}")
