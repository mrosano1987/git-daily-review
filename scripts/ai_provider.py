"""
ai_provider.py — Generic LLM provider abstraction for Git Daily Review.

Supports:
  - Anthropic  (Claude Haiku, Sonnet, Opus)
  - OpenAI     (GPT-4o, GPT-4o-mini, o1-mini, ...)
  - Ollama     (any local model — qwen2.5, llama3, mistral, ...)
  - Gemini     (gemini-1.5-flash, gemini-1.5-pro, ...)

Usage:
    provider = create_provider(config["ai"])
    response = provider.complete(prompt)

Adding a new provider:
    1. Subclass BaseProvider and implement complete()
    2. Add a case in create_provider()
    3. Add the preset in PROVIDER_PRESETS below
"""

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass


# ── Provider presets (used by wizard) ──────────────────────────────────────

PROVIDER_PRESETS = {
    "anthropic": {
        "label":       "Anthropic (Claude)",
        "env_var":     "ANTHROPIC_API_KEY",
        "key_url":     "https://console.anthropic.com/settings/keys",
        "needs_key":   True,
        "needs_url":   False,
        "models": [
            {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5 — fastest, cheapest"},
            {"id": "claude-sonnet-4-20250514",   "label": "Claude Sonnet 4 — balanced"},
            {"id": "claude-opus-4-5",            "label": "Claude Opus 4.5 — most thorough"},
        ],
        "default_model": "claude-haiku-4-5-20251001",
    },
    "openai": {
        "label":       "OpenAI (ChatGPT)",
        "env_var":     "OPENAI_API_KEY",
        "key_url":     "https://platform.openai.com/api-keys",
        "needs_key":   True,
        "needs_url":   False,
        "models": [
            {"id": "gpt-4o-mini",   "label": "GPT-4o mini — fastest, cheapest"},
            {"id": "gpt-4o",        "label": "GPT-4o — balanced"},
            {"id": "o1-mini",       "label": "o1-mini — reasoning model"},
        ],
        "default_model": "gpt-4o-mini",
    },
    "ollama": {
        "label":       "Ollama (local)",
        "env_var":     "",
        "key_url":     "https://ollama.com",
        "needs_key":   False,
        "needs_url":   True,
        "models": [
            {"id": "qwen2.5:7b",    "label": "Qwen 2.5 7B — good balance"},
            {"id": "llama3.2:3b",   "label": "Llama 3.2 3B — very fast"},
            {"id": "llama3.1:8b",   "label": "Llama 3.1 8B — quality"},
            {"id": "mistral:7b",    "label": "Mistral 7B — fast"},
            {"id": "gemma3:4b",     "label": "Gemma 3 4B — compact"},
            {"id": "deepseek-r1:7b","label": "DeepSeek R1 7B — reasoning"},
        ],
        "default_model": "qwen2.5:7b",
        "default_url":   "http://localhost:11434",
    },
    "gemini": {
        "label":       "Google Gemini",
        "env_var":     "GEMINI_API_KEY",
        "key_url":     "https://aistudio.google.com/app/apikey",
        "needs_key":   True,
        "needs_url":   False,
        "models": [
            {"id": "gemini-2.0-flash",      "label": "Gemini 2.0 Flash — fastest"},
            {"id": "gemini-1.5-flash",      "label": "Gemini 1.5 Flash — balanced"},
            {"id": "gemini-1.5-pro",        "label": "Gemini 1.5 Pro — thorough"},
        ],
        "default_model": "gemini-2.0-flash",
    },
}


# ── Base class ──────────────────────────────────────────────────────────────

class BaseProvider(ABC):
    """Common interface for all LLM providers."""

    def __init__(self, model: str, max_tokens: int = 4096):
        self.model = model
        self.max_tokens = max_tokens

    @abstractmethod
    def complete(self, prompt: str) -> str:
        """
        Send a prompt, return the text response.
        Raises RuntimeError on unrecoverable errors.
        """

    @property
    def name(self) -> str:
        return self.__class__.__name__.replace("Provider", "")


# ── Anthropic ───────────────────────────────────────────────────────────────

class AnthropicProvider(BaseProvider):

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096):
        super().__init__(model, max_tokens)
        try:
            import anthropic as _anthropic
            self._client = _anthropic.Anthropic(api_key=api_key)
        except ImportError:
            raise ImportError("pip install anthropic")

    def complete(self, prompt: str) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                block.text for block in response.content
                if hasattr(block, "text")
            )
        except Exception as e:
            raise RuntimeError(f"Anthropic API error: {e}") from e


# ── OpenAI ──────────────────────────────────────────────────────────────────

class OpenAIProvider(BaseProvider):

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096):
        super().__init__(model, max_tokens)
        self._api_key = api_key
        self._base_url = "https://api.openai.com/v1"

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
        }
        # o1 models use max_completion_tokens, not max_tokens
        if self.model.startswith("o1") or self.model.startswith("o3"):
            payload["max_completion_tokens"] = self.max_tokens
        else:
            payload["max_tokens"] = self.max_tokens

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=data,
            method="POST",
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
                return body["choices"][0]["message"]["content"]
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"OpenAI API error {e.code}: {body}") from e
        except Exception as e:
            raise RuntimeError(f"OpenAI request failed: {e}") from e


# ── Ollama ──────────────────────────────────────────────────────────────────

class OllamaProvider(BaseProvider):

    def __init__(self, model: str, base_url: str = "http://localhost:11434",
                 max_tokens: int = 4096, num_ctx: int = 32768):
        super().__init__(model, max_tokens)
        self._base_url = base_url.rstrip("/")
        # Context window esplicita: senza num_ctx Ollama usa il default del
        # modello (spesso 2048-4096 token) e TRONCA silenziosamente l'inizio
        # del prompt — cioè istruzioni e knowledge base.
        self.num_ctx = num_ctx

    def list_local_models(self) -> list[str]:
        """Return the list of models available locally in Ollama."""
        try:
            req = urllib.request.Request(
                f"{self._base_url}/api/tags", method="GET"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read())
                return [m["name"] for m in body.get("models", [])]
        except Exception:
            return []

    def complete(self, prompt: str) -> str:
        payload = {
            "model":  self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"num_predict": self.max_tokens, "num_ctx": self.num_ctx},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                body = json.loads(resp.read())
                return body.get("response", "")
        except urllib.error.HTTPError as e:
            # HTTPError is a subclass of URLError — must be caught first
            body_text = ""
            try:
                body_text = e.read().decode()
                err_json  = json.loads(body_text)
                detail    = err_json.get("error", body_text)
            except Exception:
                detail = body_text or str(e)
            if e.code == 404:
                available = self.list_local_models()
                hint = (
                    f"\n  Available models: {', '.join(available)}"
                    if available else
                    "\n  No models found. Pull one with: ollama pull qwen2.5:7b"
                )
                raise RuntimeError(
                    f"Model '{self.model}' not found in Ollama.{hint}"
                ) from e
            raise RuntimeError(f"Ollama error {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"Cannot connect to Ollama at {self._base_url}.\n"
                f"  Error: {e.reason}\n"
                "  Is Ollama running? Try: ollama serve"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Ollama request failed: {e}") from e


# ── Gemini ──────────────────────────────────────────────────────────────────

class GeminiProvider(BaseProvider):

    def __init__(self, model: str, api_key: str, max_tokens: int = 4096):
        super().__init__(model, max_tokens)
        self._api_key = api_key

    def complete(self, prompt: str) -> str:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self._api_key}"
        )
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": self.max_tokens},
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
                return (
                    body["candidates"][0]["content"]["parts"][0]["text"]
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            raise RuntimeError(f"Gemini API error {e.code}: {body}") from e
        except Exception as e:
            raise RuntimeError(f"Gemini request failed: {e}") from e


# ── Factory ─────────────────────────────────────────────────────────────────

def create_provider(ai_config: dict) -> BaseProvider:
    """
    Instantiate the correct provider from the 'ai' section of config.yaml.

    Expected config keys:
        provider   : "anthropic" | "openai" | "ollama" | "gemini"
        model      : model identifier string
        api_key_env: name of the env var holding the API key (optional)
        base_url   : base URL for Ollama (optional)
        max_tokens : int (default 4096)
    """
    provider_name = ai_config.get("provider", "anthropic").lower()
    model         = ai_config.get("model", "")
    max_tokens    = int(ai_config.get("max_tokens", 4096))
    base_url      = ai_config.get("base_url", "")

    # Resolve API key from explicit env var name, or fallback defaults
    key_env  = ai_config.get("api_key_env", "")
    api_key  = os.environ.get(key_env, "") if key_env else ""

    if not api_key:
        # Try the conventional env var for each provider
        defaults = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai":    "OPENAI_API_KEY",
            "gemini":    "GEMINI_API_KEY",
        }
        api_key = os.environ.get(defaults.get(provider_name, ""), "")

    if provider_name == "anthropic":
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set.\n"
                "  export ANTHROPIC_API_KEY='sk-ant-...'"
            )
        preset = PROVIDER_PRESETS["anthropic"]
        model  = model or preset["default_model"]
        return AnthropicProvider(model, api_key, max_tokens)

    elif provider_name == "openai":
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY not set.\n"
                "  export OPENAI_API_KEY='sk-...'"
            )
        preset = PROVIDER_PRESETS["openai"]
        model  = model or preset["default_model"]
        return OpenAIProvider(model, api_key, max_tokens)

    elif provider_name == "ollama":
        preset   = PROVIDER_PRESETS["ollama"]
        model    = model or preset["default_model"]
        base_url = base_url or preset["default_url"]
        num_ctx  = int(ai_config.get("num_ctx", 32768))
        return OllamaProvider(model, base_url, max_tokens, num_ctx)

    elif provider_name == "gemini":
        if not api_key:
            raise RuntimeError(
                "GEMINI_API_KEY not set.\n"
                "  export GEMINI_API_KEY='AI...'"
            )
        preset = PROVIDER_PRESETS["gemini"]
        model  = model or preset["default_model"]
        return GeminiProvider(model, api_key, max_tokens)

    else:
        raise ValueError(
            f"Unknown provider: '{provider_name}'. "
            f"Supported: {list(PROVIDER_PRESETS.keys())}"
        )


def test_provider(ai_config: dict) -> dict:
    """
    Connectivity test for a provider config.
    For Ollama: checks reachability and model availability separately
    before attempting a completion, to give actionable error messages.
    Returns {"ok": True, "provider": ..., "model": ...} or {"ok": False, "message": ...}.
    """
    try:
        provider = create_provider(ai_config)

        # Ollama-specific pre-flight checks
        if isinstance(provider, OllamaProvider):
            # 1. Check server reachability via /api/tags
            available = provider.list_local_models()
            if available is None:
                return {
                    "ok": False,
                    "message": (
                        f"Cannot connect to Ollama at {provider._base_url}.\n"
                        "Make sure Ollama is running."
                    )
                }
            # 2. Check if the requested model is available
            model_base = provider.model.split(":")[0]
            matched = [m for m in available
                       if m == provider.model or m.startswith(model_base + ":")]
            if not matched:
                hint = (
                    f"Available: {', '.join(available)}"
                    if available else
                    "No models found locally."
                )
                return {
                    "ok": False,
                    "message": (
                        f"Model '{provider.model}' is not pulled yet.\n"
                        f"{hint}\n"
                        f"Pull it with: ollama pull {provider.model}"
                    )
                }

        # Full completion test (all providers)
        response = provider.complete("Reply with exactly one word: OK")
        if response.strip():
            return {"ok": True, "provider": provider.name, "model": provider.model}
        return {"ok": False, "message": "Empty response from provider"}

    except RuntimeError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"Unexpected error: {e}"}