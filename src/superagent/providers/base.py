"""Provider interfaces. The application depends on these, never on a vendor.

A provider that lacks credentials reports ``NOT_CONFIGURED``; one that lacks an
optional package reports ``UNAVAILABLE``. Neither prevents the app from booting.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..errors import ProviderError
from ..types import ProviderKind, ProviderStatus, ProviderTier


@dataclass(frozen=True)
class Readiness:
    ok: bool
    reason: str = ""
    status: ProviderStatus = ProviderStatus.AVAILABLE

    @classmethod
    def ready(cls) -> Readiness:
        return cls(True)

    @classmethod
    def not_configured(cls, reason: str) -> Readiness:
        return cls(False, reason, ProviderStatus.NOT_CONFIGURED)

    @classmethod
    def unavailable(cls, reason: str) -> Readiness:
        return cls(False, reason, ProviderStatus.UNAVAILABLE)


class ProviderHealth(BaseModel):
    name: str
    kind: ProviderKind
    tier: ProviderTier
    status: ProviderStatus
    detail: str = ""
    model: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    cooldown_seconds: float = 0.0
    consecutive_failures: int = 0
    last_success_at: datetime | None = None


class BaseProvider(ABC):
    name: str = "provider"
    kind: ProviderKind
    tier: ProviderTier = ProviderTier.CLOUD_PRIMARY
    capabilities: frozenset[str] = frozenset()
    model: str | None = None
    description: str = ""

    @abstractmethod
    def readiness(self) -> Readiness:
        """Static check: credentials, binaries and optional packages. No network."""

    async def probe(self) -> None:  # noqa: B027 - optional hook
        """Cheap live check. Raise ``ProviderError`` when the endpoint is not usable."""

    async def aclose(self) -> None:  # noqa: B027 - optional hook
        """Release pooled connections."""

    def unsupported(self, operation: str) -> ProviderError:
        return ProviderError(
            self.name, ProviderStatus.UNAVAILABLE, f"does not support '{operation}'", penalize=False
        )


# --------------------------------------------------------------------------- LLM
class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    messages: list[ChatMessage]
    system: str | None = None
    max_tokens: int = 4096
    json_mode: bool = False
    json_schema: dict[str, Any] | None = None
    images: list[Path] = Field(default_factory=list)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    text: str
    provider: str
    model: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str = "stop"


class LLMProvider(BaseProvider):
    """Capabilities: chat, vision."""

    kind = ProviderKind.LLM
    capabilities = frozenset({"chat"})

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


# --------------------------------------------------------------------- embedding
class EmbeddingProvider(BaseProvider):
    kind = ProviderKind.EMBEDDING
    dim: int = 0

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


# ------------------------------------------------------------------------- image
class ImageRequest(BaseModel):
    prompt: str
    negative_prompt: str = ""
    width: int = 1024
    height: int = 1024
    seed: int | None = None
    num_images: int = 1
    reference_images: list[Path] = Field(default_factory=list)
    mask: Path | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class ImageResult(BaseModel):
    path: Path
    provider: str
    model: str | None = None
    seed: int | None = None
    width: int
    height: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class ImageProvider(BaseProvider):
    """Capabilities: generate, edit, inpaint, outpaint, upscale, reference."""

    kind = ProviderKind.IMAGE
    capabilities = frozenset({"generate"})

    @abstractmethod
    async def generate(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]: ...

    async def generate_variations(self, request: ImageRequest, out_dir: Path, count: int) -> list[ImageResult]:
        """Default: ``count`` independent generations with distinct seeds."""
        base_seed = request.seed if request.seed is not None else 1000
        results: list[ImageResult] = []
        for index in range(count):
            variant = request.model_copy(update={"seed": base_seed + index * 7919, "num_images": 1})
            results.extend(await self.generate(variant, out_dir))
        return results

    async def create_reference(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        """A reference sheet is a normal generation whose output conditions later scenes."""
        return await self.generate(request, out_dir)

    async def edit(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        raise self.unsupported("edit")

    async def inpaint(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        raise self.unsupported("inpaint")

    async def outpaint(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        raise self.unsupported("outpaint")

    async def upscale(self, image: Path, out_dir: Path, scale: int = 2) -> ImageResult:
        raise self.unsupported("upscale")


# ------------------------------------------------------------------------- video
class MotionPlan(BaseModel):
    """Camera and subject motion for an image-to-video shot."""

    camera: str = "push-in"
    intensity: float = Field(0.5, ge=0.0, le=1.0)
    subject: str = ""
    environment: str = ""


class VideoRequest(BaseModel):
    prompt: str = ""
    negative_prompt: str = ""
    image: Path | None = None
    video: Path | None = None
    duration: float = 5.0
    fps: int = 30
    width: int = 1080
    height: int = 1920
    seed: int | None = None
    motion: MotionPlan = Field(default_factory=MotionPlan)
    extra: dict[str, Any] = Field(default_factory=dict)


class VideoResult(BaseModel):
    path: Path
    provider: str
    model: str | None = None
    duration: float | None = None
    generative: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class VideoProvider(BaseProvider):
    """Capabilities: text_to_video, image_to_video, video_to_video, video_extend, video_variation."""

    kind = ProviderKind.VIDEO

    async def text_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        raise self.unsupported("text_to_video")

    async def image_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        raise self.unsupported("image_to_video")

    async def video_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        raise self.unsupported("video_to_video")

    async def video_extend(self, request: VideoRequest, out_path: Path) -> VideoResult:
        raise self.unsupported("video_extend")

    async def video_variation(self, request: VideoRequest, out_path: Path) -> VideoResult:
        raise self.unsupported("video_variation")


# ------------------------------------------------------------------------- voice
class VoiceInfo(BaseModel):
    id: str
    name: str
    language: str = ""
    gender: str = ""


class VoiceRequest(BaseModel):
    text: str
    voice: str | None = None
    language: str = "en"
    speed: float = Field(1.0, gt=0.25, le=3.0)


class DialogueLine(BaseModel):
    speaker: str
    text: str


class VoiceResult(BaseModel):
    path: Path
    provider: str
    voice: str | None = None
    duration: float | None = None


class VoiceProvider(BaseProvider):
    kind = ProviderKind.VOICE
    capabilities = frozenset({"text_to_speech"})

    @abstractmethod
    async def text_to_speech(self, request: VoiceRequest, out_path: Path) -> VoiceResult: ...

    @abstractmethod
    async def list_voices(self) -> list[VoiceInfo]: ...

    def default_voice(self, language: str) -> str | None:
        return None
