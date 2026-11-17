"""Local LLM client for Ollama (free; runs on this laptop). Talks to Ollama's REST API with httpx.

    GET  /api/tags   which models are installed
    POST /api/chat   chat with tool calling (non-streaming)
    POST /api/embed  embeddings

We never assume Ollama is installed or running: every failure becomes a typed error with a
message that says exactly what to do.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.config import settings


class OllamaError(Exception):
    """Base class: anything that stops us using the local LLM."""


class OllamaUnavailable(OllamaError):
    """Ollama isn't installed/running or didn't answer in time."""


class OllamaModelMissing(OllamaError):
    """Ollama runs but the configured model isn't pulled."""


def _model_matches(installed: str, wanted: str) -> bool:
    if installed == wanted:
        return True
    if ":" not in wanted:  # "qwen2.5" means "qwen2.5:latest"
        return installed == f"{wanted}:latest"
    return False


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None,
                 timeout: float | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        self.model = model or settings.OLLAMA_MODEL
        self._http = httpx.Client(base_url=self.base_url,
                                  timeout=httpx.Timeout(timeout or settings.OLLAMA_TIMEOUT, connect=3.0),
                                  transport=transport)

    # -- helpers ------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._http.request(method, path, **kwargs)
        except httpx.ConnectError as exc:
            raise OllamaUnavailable(
                f"Ollama is not running at {self.base_url}. Install it from https://ollama.com, "
                "then start it (open the Ollama app, or run `ollama serve`).") from exc
        except httpx.TimeoutException as exc:
            raise OllamaUnavailable(
                f"Ollama at {self.base_url} did not answer in time (the model may still be loading, "
                "or this laptop is too slow for it). Try a smaller model.") from exc
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(f"Could not talk to Ollama: {exc}") from exc

    # -- public API ----------------------------------------------------------
    def list_models(self) -> list[str]:
        resp = self._request("GET", "/api/tags")
        if resp.status_code != 200:
            raise OllamaUnavailable(f"Ollama /api/tags returned HTTP {resp.status_code}.")
        return [m.get("name", "") for m in resp.json().get("models", [])]

    def status(self) -> dict[str, Any]:
        """{'available': bool, 'reason': str, 'model': str, 'models': [...]} — never raises."""
        try:
            models = self.list_models()
        except OllamaError as exc:
            return {"available": False, "reason": str(exc), "model": self.model, "models": []}
        if not any(_model_matches(m, self.model) for m in models):
            return {"available": False, "model": self.model, "models": models,
                    "reason": f"Ollama is running but model '{self.model}' is not installed. "
                              f"Run: ollama pull {self.model}"}
        return {"available": True, "reason": f"Ollama model {self.model}", "model": self.model, "models": models}

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """One non-streaming chat call. Returns Ollama's `message` dict
        ({'role': 'assistant', 'content': str, 'tool_calls': [...]})."""
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "stream": False,
                                   "options": {"temperature": 0.2}}
        if tools:
            payload["tools"] = tools
        resp = self._request("POST", "/api/chat", json=payload)
        if resp.status_code == 404:
            raise OllamaModelMissing(f"Model '{self.model}' is not installed. Run: ollama pull {self.model}")
        if resp.status_code >= 400:
            detail = _error_text(resp)
            if "does not support tools" in detail:
                raise OllamaError(f"Model '{self.model}' does not support tool calling. "
                                  "Use one that does, e.g. qwen2.5:3b, qwen2.5:7b or llama3.1:8b.")
            raise OllamaError(f"Ollama error {resp.status_code}: {detail}")
        message = resp.json().get("message") or {}
        for call in message.get("tool_calls") or []:  # some versions send arguments as a JSON string
            fn = call.get("function", {})
            if isinstance(fn.get("arguments"), str):
                try:
                    fn["arguments"] = json.loads(fn["arguments"] or "{}")
                except json.JSONDecodeError:
                    fn["arguments"] = {}
        return message

    def embed(self, text: str, model: str) -> list[float]:
        resp = self._request("POST", "/api/embed", json={"model": model, "input": text})
        if resp.status_code == 404:
            raise OllamaModelMissing(f"Embedding model '{model}' is not installed. Run: ollama pull {model}")
        if resp.status_code >= 400:
            raise OllamaError(f"Ollama embed error {resp.status_code}: {_error_text(resp)}")
        vectors = resp.json().get("embeddings") or []
        if not vectors:
            raise OllamaError("Ollama returned no embedding.")
        return [float(x) for x in vectors[0]]


def _error_text(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("error", resp.text))
    except ValueError:
        return resp.text[:300]


# Cache the health check briefly so every chat message doesn't re-probe Ollama.
_status_cache: dict[str, Any] = {"at": 0.0, "value": None}


def cached_status(client: OllamaClient | None = None, ttl: float = 20.0) -> dict[str, Any]:
    now = time.monotonic()
    if client is None and _status_cache["value"] is not None and now - _status_cache["at"] < ttl:
        return _status_cache["value"]
    value = (client or OllamaClient()).status()
    if client is None:
        _status_cache.update(at=now, value=value)
    return value
