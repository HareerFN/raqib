"""LLM client for OpenRouter (OpenAI-compatible), standard library only.

Cost and reproducibility features:
  - Response cache on disk, keyed by a hash of (model, messages, params).
    Re-running a benchmark replays cached answers: same results, zero cost.
  - Every call records tokens and cost, so the benchmark can report cost per incident.
  - temperature 0 by default.

Setup: put your key in a file named .env at the project root:
    OPENROUTER_API_KEY=sk-or-...
"""

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".llm_cache"
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Choose models here (any OpenRouter model id works). Strong for diagnosis, cheap for simple steps.
MODELS = {
    "strong": os.environ.get("RAQIB_MODEL_STRONG", "deepseek/deepseek-chat-v3.1"),
    "cheap": os.environ.get("RAQIB_MODEL_CHEAP", "openai/gpt-4o-mini"),
}


def _load_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


class LLMClient:
    def __init__(self, tier: str = "strong", use_cache: bool = True):
        _load_env()
        self.model = MODELS.get(tier, tier)
        self.use_cache = use_cache
        self.calls = 0
        self.cost_usd = 0.0
        self.tokens = 0
        CACHE_DIR.mkdir(exist_ok=True)

    def chat(self, messages: list[dict], max_tokens: int = 800, temperature: float = 0.0,
             json_mode: bool = True) -> str:
        payload = {"model": self.model, "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "usage": {"include": True}}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:32]
        cache_file = CACHE_DIR / f"{key}.json"
        if self.use_cache and cache_file.exists():
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            self.calls += 1
            return data["content"]  # cached answers cost nothing

        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY missing: add it to .env at the project root")
        req = urllib.request.Request(
            API_URL, data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                     "X-Title": "Raqib recovery agent"})
        body = None
        for attempt in range(5):
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    body = json.loads(resp.read())
                if not body.get("choices"):
                    raise ValueError(f"no choices in reply: {str(body)[:200]}")
                break
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 402, 403) or attempt == 4:   # no credit or no access: retrying will not help
                    raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
                time.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
                # dropped connections and truncated or malformed replies happen on long runs
                if attempt == 4:
                    raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
                time.sleep(2 ** attempt)
        content = body["choices"][0]["message"]["content"]
        usage = body.get("usage", {})
        cost = float(usage.get("cost", 0) or 0)
        self.calls += 1
        self.cost_usd += cost
        self.tokens += int(usage.get("total_tokens", 0) or 0)
        cache_file.write_text(json.dumps({"model": self.model, "content": content, "usage": usage}),
                              encoding="utf-8")
        return content


class FakeLLM:
    """Offline stand-in for tests: answers from a function instead of a model."""

    def __init__(self, responder):
        self.responder = responder
        self.calls = 0
        self.cost_usd = 0.0
        self.tokens = 0
        self.model = "fake"

    def chat(self, messages, **_):
        self.calls += 1
        return self.responder(messages)


def parse_json(text: str) -> dict:
    """Tolerates code fences and leading prose around a JSON object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[1] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1])
