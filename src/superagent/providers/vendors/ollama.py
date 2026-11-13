"""Ollama: the default local LLM and embedding backend."""

from __future__ import annotations

from typing import Any

import httpx

from ...errors import ProviderError
from ...media.images import image_b64
from ...types import ProviderStatus, ProviderTier
from ..base import EmbeddingProvider, LLMProvider, LLMRequest, LLMResponse, Readiness, Usage
from ..http import json_body, new_client, send

DEFAULT_URL = "http://127.0.0.1:11434"
_NOT_CONFIGURED = "Ollama is not running at the default address and OLLAMA_BASE_URL is not set"


class _OllamaBase:
    name: str

    def __init__(self, base_url: str | None, model: str | None):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            # local generation on CPU can take minutes
            self._client = new_client(timeout=httpx.Timeout(900.0, connect=5.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def installed_models(self) -> list[str]:
        response = await send(self.name, self.client, "GET", f"{self.base_url}/api/tags")
        return [m.get("name", "") for m in json_body(self.name, response).get("models", []) if m.get("name")]


class OllamaLLM(_OllamaBase, LLMProvider):
    name = "ollama"
    tier = ProviderTier.LOCAL
    capabilities = frozenset({"chat", "vision"})
    description = "local models served by Ollama"

    def readiness(self) -> Readiness:
        return Readiness.ready() if self.base_url else Readiness.not_configured(_NOT_CONFIGURED)

    async def probe(self) -> None:
        await self._resolve_model()

    async def _resolve_model(self) -> str:
        if self.model:
            return self.model
        models = [m for m in await self.installed_models() if "embed" not in m]
        if not models:
            raise ProviderError(
                self.name, ProviderStatus.UNAVAILABLE, "no chat model is pulled",
                hint="run `ollama pull <model>` or set OLLAMA_MODEL",
            )  # fmt: skip
        self.model = models[0]
        return self.model

    async def complete(self, request: LLMRequest) -> LLMResponse:
        model = await self._resolve_model()
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)
        if request.images:
            target = next((m for m in reversed(messages) if m["role"] == "user"), None)
            if target is not None:
                target["images"] = [image_b64(path)[1] for path in request.images]
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False, "options": {"num_predict": request.max_tokens},
        }  # fmt: skip
        if request.json_mode or request.json_schema is not None:
            body["format"] = "json"
        response = await send(self.name, self.client, "POST", f"{self.base_url}/api/chat", json=body)
        data = json_body(self.name, response)
        text = str((data.get("message") or {}).get("content") or "")
        if not text.strip():
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "the model returned an empty reply", penalize=False)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(data.get("model") or model),
            usage=Usage(
                input_tokens=int(data.get("prompt_eval_count") or 0), output_tokens=int(data.get("eval_count") or 0)
            ),
            finish_reason=str(data.get("done_reason") or "stop"),
        )


class OllamaEmbedding(_OllamaBase, EmbeddingProvider):
    name = "ollama-embed"
    tier = ProviderTier.LOCAL
    description = "local embeddings served by Ollama"

    def readiness(self) -> Readiness:
        if not self.base_url:
            return Readiness.not_configured(_NOT_CONFIGURED)
        if not self.model:
            return Readiness.not_configured("no embedding model selected (set OLLAMA_EMBED_MODEL)")
        return Readiness.ready()

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await send(
            self.name, self.client, "POST", f"{self.base_url}/api/embed", json={"model": self.model, "input": texts}
        )
        vectors = json_body(self.name, response).get("embeddings") or []
        if len(vectors) != len(texts):
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "embedding count mismatch")
        self.dim = len(vectors[0]) if vectors else 0
        return vectors


async def detect(base_url: str = DEFAULT_URL, timeout: float = 0.6) -> bool:
    """True when an Ollama server answers at ``base_url``. Used for local-first autodetection."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base_url}/api/tags")
            return response.status_code == 200
    except httpx.HTTPError:
        return False
