"""Claude via the official Anthropic SDK (optional dependency: ``pip install 'super-agent-x[anthropic]'``).

Notes that shape this adapter:
* current models think adaptively by default, so no ``thinking`` parameter is sent;
  depth is controlled with ``output_config.effort``;
* sampling parameters (temperature/top_p/top_k) are no longer accepted and are never sent;
* a safety decline arrives as HTTP 200 with ``stop_reason == "refusal"``; on models that
  support it the server-side fallback is enabled so the API can re-run the request itself.
"""

from __future__ import annotations

import importlib.util
from typing import Any

from ...errors import ProviderError
from ...media.images import image_b64
from ...types import ProviderStatus, ProviderTier
from ..base import LLMProvider, LLMRequest, LLMResponse, Readiness, Usage

_FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept the server-side `fallbacks` parameter.
_FALLBACK_MODELS = {
    "claude-fable-5-2", "claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5",
}  # fmt: skip
_NON_STREAMING_MAX_TOKENS = 16000


class AnthropicLLM(LLMProvider):
    name = "anthropic"
    tier = ProviderTier.CLOUD_PRIMARY
    capabilities = frozenset({"chat", "vision"})

    def __init__(self, api_key: str | None, model: str, *, effort: str = "medium", refusal_fallback: bool = True):
        self.api_key = api_key
        self.model = model
        self.effort = effort.strip().lower()
        self.refusal_fallback = refusal_fallback
        self.description = "Claude via the Anthropic API"
        self._client: Any = None
        self._structured = True

    def readiness(self) -> Readiness:
        if not self.api_key:
            return Readiness.not_configured("no API key (set ANTHROPIC_API_KEY)")
        if importlib.util.find_spec("anthropic") is None:
            return Readiness.unavailable("the 'anthropic' package is not installed (pip install 'super-agent-x[anthropic]')")
        return Readiness.ready()

    @property
    def client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.AsyncAnthropic(api_key=self.api_key, timeout=300.0, max_retries=1)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def probe(self) -> None:
        import anthropic

        try:
            await self.client.models.retrieve(self.model)
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc

    async def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic

        messages: list[dict[str, Any]] = [
            {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
        ]
        if request.images:
            target = next((m for m in reversed(messages) if m["role"] == "user"), None)
            if target is not None:
                blocks: list[dict[str, Any]] = []
                for path in request.images:
                    media_type, data = image_b64(path)
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
                blocks.append({"type": "text", "text": target["content"]})
                target["content"] = blocks
        system_parts = [request.system] if request.system else []
        system_parts += [m.content for m in request.messages if m.role == "system"]

        while True:
            kwargs = self._kwargs(request, messages, system_parts)
            try:
                response = await self._send(kwargs)
            except anthropic.BadRequestError as exc:
                # A schema the structured-output grammar cannot express: fall back to prompted JSON.
                if self._structured and request.json_schema is not None and _mentions_schema(str(exc)):
                    self._structured = False
                    continue
                raise self._translate(exc) from exc
            except anthropic.APIError as exc:
                raise self._translate(exc) from exc
            break

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) or "unspecified"
            raise ProviderError(
                self.name, ProviderStatus.UNAVAILABLE, f"the model declined this request (category: {category})",
                penalize=False,
            )  # fmt: skip
        text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
        if not text.strip():
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "the model returned no text", penalize=False)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(response.model),
            usage=Usage(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens),
            finish_reason="length" if response.stop_reason == "max_tokens" else "stop",
        )

    # ------------------------------------------------------------- internals
    def _kwargs(self, request: LLMRequest, messages: list[dict[str, Any]], system_parts: list[str]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"model": self.model, "max_tokens": request.max_tokens, "messages": messages}
        if system_parts:
            kwargs["system"] = "\n\n".join(system_parts)
        output_config: dict[str, Any] = {}
        if self.effort and self.effort != "none":
            output_config["effort"] = self.effort
        if request.json_schema is not None and self._structured:
            output_config["format"] = {"type": "json_schema", "schema": request.json_schema}
        if output_config:
            kwargs["output_config"] = output_config
        return kwargs

    async def _send(self, kwargs: dict[str, Any]) -> Any:
        use_fallback = self.refusal_fallback and self.model in _FALLBACK_MODELS
        messages_api = self.client.beta.messages if use_fallback else self.client.messages
        if use_fallback:
            kwargs = {**kwargs, "betas": [_FALLBACK_BETA], "fallbacks": "default"}
        if kwargs["max_tokens"] > _NON_STREAMING_MAX_TOKENS:
            # large outputs must stream or the HTTP request can time out
            async with messages_api.stream(**kwargs) as stream:
                return await stream.get_final_message()
        return await messages_api.create(**kwargs)

    def _translate(self, exc: Exception) -> ProviderError:
        import anthropic

        message = str(getattr(exc, "message", None) or exc)[:400]
        if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
            return ProviderError(self.name, ProviderStatus.AUTH_ERROR, message, hint="check ANTHROPIC_API_KEY")
        if isinstance(exc, anthropic.RateLimitError):
            raw = exc.response.headers.get("retry-after") if exc.response is not None else None
            try:
                retry_after = float(raw) if raw else None
            except ValueError:
                retry_after = None
            return ProviderError(self.name, ProviderStatus.RATE_LIMITED, message, retry_after=retry_after)
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderError(self.name, ProviderStatus.UNAVAILABLE, message, hint="check ANTHROPIC_MODEL")
        if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
            return ProviderError(self.name, ProviderStatus.NETWORK_ERROR, message)
        if isinstance(exc, anthropic.APIStatusError):
            if exc.status_code >= 500 or exc.status_code in (408, 409):
                return ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, message)
            return ProviderError(self.name, ProviderStatus.UNAVAILABLE, message, penalize=False)
        return ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, message)


def _mentions_schema(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in ("schema", "output_config", "format"))
