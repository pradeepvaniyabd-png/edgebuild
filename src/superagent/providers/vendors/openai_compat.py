"""OpenAI-compatible endpoints: OpenAI itself, plus any gateway or local server that speaks
the same protocol (OmniRoute, FreeLLMAPI, LM Studio, LocalAI, vLLM, Ollama's /v1 ...)."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx

from ...errors import ProviderError
from ...media.images import fit_image, image_b64
from ...types import ProviderStatus, ProviderTier
from ...utils import stable_hash
from ..base import (
    EmbeddingProvider,
    ImageProvider,
    ImageRequest,
    ImageResult,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Readiness,
    Usage,
    VoiceInfo,
    VoiceProvider,
    VoiceRequest,
    VoiceResult,
)
from ..http import download, json_body, new_client, send


class _Endpoint:
    """Connection settings shared by every capability of one OpenAI-compatible endpoint."""

    def __init__(self, *, label: str, base_url: str | None, api_key: str | None, requires_key: bool, key_hint: str):
        self.label = label
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.requires_key = requires_key
        self.key_hint = key_hint
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            self._client = new_client(headers=headers, timeout=httpx.Timeout(300.0, connect=10.0))
        return self._client

    def readiness(self, model: str | None, model_hint: str) -> Readiness:
        if not self.base_url:
            return Readiness.not_configured(f"no endpoint URL ({self.key_hint})")
        if self.requires_key and not self.api_key:
            return Readiness.not_configured(f"no API key ({self.key_hint})")
        if not model:
            return Readiness.not_configured(f"no model selected (set {model_hint})")
        return Readiness.ready()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def make_endpoint(
    *, label: str, base_url: str | None, api_key: str | None, requires_key: bool, key_hint: str
) -> _Endpoint:
    return _Endpoint(label=label, base_url=base_url, api_key=api_key, requires_key=requires_key, key_hint=key_hint)


class OpenAICompatLLM(LLMProvider):
    capabilities = frozenset({"chat", "vision"})

    def __init__(self, endpoint: _Endpoint, *, name: str, model: str | None, tier: ProviderTier, model_hint: str):
        self.endpoint = endpoint
        self.name = name
        self.model = model
        self.tier = tier
        self._model_hint = model_hint
        self.description = f"chat completions at {endpoint.base_url or '(unset)'}"
        # api.openai.com rejects max_tokens on current models; most compatible servers expect it.
        self._length_field = "max_completion_tokens" if "api.openai.com" in endpoint.base_url else "max_tokens"
        self._json_format = True

    def readiness(self) -> Readiness:
        return self.endpoint.readiness(self.model, self._model_hint)

    async def probe(self) -> None:
        await send(self.name, self.endpoint.client, "GET", f"{self.endpoint.base_url}/models")

    async def aclose(self) -> None:
        await self.endpoint.aclose()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        messages: list[dict[str, Any]] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)
        if request.images:
            _attach_images(messages, request.images)
        wants_json = request.json_mode or request.json_schema is not None

        data: dict[str, Any] = {}
        adaptations = 0
        while True:
            body: dict[str, Any] = {"model": self.model, "messages": messages, self._length_field: request.max_tokens}
            if wants_json and self._json_format:
                body["response_format"] = {"type": "json_object"}
            try:
                response = await send(
                    self.name, self.endpoint.client, "POST", f"{self.endpoint.base_url}/chat/completions", json=body
                )
            except ProviderError as exc:
                # A 4xx about our dialect (token parameter, response_format) is worth one adjusted retry each.
                if exc.penalize or adaptations >= 2 or not self._adapt(exc.message):
                    raise
                adaptations += 1
                continue
            data = json_body(self.name, response)
            break

        choices = data.get("choices") or []
        if not choices:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "response contained no choices")
        message = choices[0].get("message") or {}
        finish = str(choices[0].get("finish_reason") or "stop")
        text = _content_text(message.get("content"))
        if message.get("refusal") or finish == "content_filter":
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "the model declined this request", penalize=False)
        if not text.strip():
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "the model returned an empty reply", penalize=False)
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=str(data.get("model") or self.model),
            usage=Usage(
                input_tokens=int(usage.get("prompt_tokens") or 0), output_tokens=int(usage.get("completion_tokens") or 0)
            ),
            finish_reason=finish,
        )

    def _adapt(self, message: str) -> bool:
        """Adjust to a server's dialect after a 4xx. Returns True when a retry is worthwhile."""
        lowered = message.lower()
        if "max_completion_tokens" in lowered and self._length_field == "max_tokens":
            self._length_field = "max_completion_tokens"
            return True
        if "max_tokens" in lowered and self._length_field == "max_completion_tokens" and "unsupported" in lowered:
            self._length_field = "max_tokens"
            return True
        if "response_format" in lowered and self._json_format:
            self._json_format = False
            return True
        return False


def _attach_images(messages: list[dict[str, Any]], images: list[Path]) -> None:
    target = next((m for m in reversed(messages) if m["role"] == "user"), None)
    if target is None:
        return
    parts: list[dict[str, Any]] = [{"type": "text", "text": target["content"]}]
    for path in images:
        media_type, data = image_b64(path)
        parts.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}})
    target["content"] = parts


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


class OpenAICompatEmbedding(EmbeddingProvider):
    def __init__(self, endpoint: _Endpoint, *, name: str, model: str | None, tier: ProviderTier, model_hint: str):
        self.endpoint = endpoint
        self.name = name
        self.model = model
        self.tier = tier
        self._model_hint = model_hint
        self.description = f"embeddings at {endpoint.base_url or '(unset)'}"

    def readiness(self) -> Readiness:
        return self.endpoint.readiness(self.model, self._model_hint)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        response = await send(
            self.name, self.endpoint.client, "POST", f"{self.endpoint.base_url}/embeddings",
            json={"model": self.model, "input": texts},
        )  # fmt: skip
        rows = sorted(json_body(self.name, response).get("data") or [], key=lambda row: row.get("index", 0))
        vectors = [row["embedding"] for row in rows]
        if len(vectors) != len(texts):
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "embedding count mismatch")
        self.dim = len(vectors[0]) if vectors else 0
        return vectors


_DEFAULT_SIZES = ["1024x1024", "1024x1536", "1536x1024"]


class OpenAICompatImage(ImageProvider):
    capabilities = frozenset({"generate", "edit", "inpaint"})

    def __init__(
        self, endpoint: _Endpoint, *, name: str, model: str | None, tier: ProviderTier, model_hint: str,
        sizes: list[str] | None = None,
    ):  # fmt: skip
        self.endpoint = endpoint
        self.name = name
        self.model = model
        self.tier = tier
        self._model_hint = model_hint
        self.sizes = sizes or _DEFAULT_SIZES
        self.description = f"image generation at {endpoint.base_url or '(unset)'}"

    def readiness(self) -> Readiness:
        return self.endpoint.readiness(self.model, self._model_hint)

    def _size(self, width: int, height: int) -> str:
        target = width / height

        def distance(size: str) -> float:
            w, h = (int(part) for part in size.split("x"))
            return abs(w / h - target)

        return min(self.sizes, key=distance)

    async def generate(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        body = {
            "model": self.model, "prompt": _with_negative(request), "n": request.num_images,
            "size": self._size(request.width, request.height), **request.extra,
        }  # fmt: skip
        response = await send(
            self.name, self.endpoint.client, "POST", f"{self.endpoint.base_url}/images/generations", json=body
        )
        return await self._collect(json_body(self.name, response), request, out_dir)

    async def edit(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        if not request.reference_images:
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "edit needs a source image", penalize=False)
        source = request.reference_images[0]
        files: dict[str, Any] = {"image": (source.name, source.read_bytes(), "image/png")}
        if request.mask is not None:
            files["mask"] = (request.mask.name, request.mask.read_bytes(), "image/png")
        form = {"model": str(self.model), "prompt": request.prompt, "n": str(request.num_images),
                "size": self._size(request.width, request.height)}  # fmt: skip
        response = await send(
            self.name, self.endpoint.client, "POST", f"{self.endpoint.base_url}/images/edits", data=form, files=files
        )
        return await self._collect(json_body(self.name, response), request, out_dir)

    async def inpaint(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        if request.mask is None:
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "inpaint needs a mask", penalize=False)
        return await self.edit(request, out_dir)

    async def _collect(self, data: dict[str, Any], request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        items = data.get("data") or []
        if not items:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "endpoint returned no images")
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = stable_hash({"p": request.prompt, "s": request.seed, "r": data.get("created")}, 10)
        results: list[ImageResult] = []
        for index, item in enumerate(items):
            dest = out_dir / f"{self.name}-{tag}-{index}.png"
            if item.get("b64_json"):
                dest.write_bytes(base64.b64decode(item["b64_json"]))
            elif item.get("url"):
                await download(self.name, self.endpoint.client, item["url"], dest, max_bytes=60_000_000)
            else:
                raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "image item had neither data nor URL")
            fit_image(dest, request.width, request.height)
            results.append(
                ImageResult(path=dest, provider=self.name, model=self.model, seed=request.seed,
                            width=request.width, height=request.height,
                            metadata={"revised_prompt": item.get("revised_prompt")})  # fmt: skip
            )
        return results


def _with_negative(request: ImageRequest) -> str:
    if not request.negative_prompt:
        return request.prompt
    return f"{request.prompt}\n\nAvoid: {request.negative_prompt}"


class OpenAICompatVoice(VoiceProvider):
    def __init__(
        self, endpoint: _Endpoint, *, name: str, model: str | None, voice: str | None, tier: ProviderTier,
        model_hint: str,
    ):  # fmt: skip
        self.endpoint = endpoint
        self.name = name
        self.model = model
        self.voice = voice
        self.tier = tier
        self._model_hint = model_hint
        self.description = f"speech synthesis at {endpoint.base_url or '(unset)'}"

    def readiness(self) -> Readiness:
        ready = self.endpoint.readiness(self.model, self._model_hint)
        if ready.ok and not self.voice:
            return Readiness.not_configured(f"no voice selected (set {self._model_hint.replace('MODEL', 'VOICE')})")
        return ready

    def default_voice(self, language: str) -> str | None:
        return self.voice

    async def list_voices(self) -> list[VoiceInfo]:
        return [VoiceInfo(id=self.voice, name=self.voice)] if self.voice else []

    async def text_to_speech(self, request: VoiceRequest, out_path: Path) -> VoiceResult:
        suffix = out_path.suffix.lstrip(".").lower() or "mp3"
        body = {
            "model": self.model, "voice": request.voice or self.voice, "input": request.text,
            "response_format": suffix if suffix in {"mp3", "wav", "flac", "opus", "aac"} else "mp3",
            "speed": request.speed,
        }  # fmt: skip
        response = await send(
            self.name, self.endpoint.client, "POST", f"{self.endpoint.base_url}/audio/speech", json=body
        )
        if not response.content:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "endpoint returned empty audio")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(response.content)
        return VoiceResult(path=out_path, provider=self.name, voice=str(body["voice"]))
