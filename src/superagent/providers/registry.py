"""Builds the provider router from settings.

Every provider is always registered; one without credentials simply reports
NOT_CONFIGURED. That keeps `super-agent providers` an honest map of what could work
and what is missing, and lets the app boot with zero keys.
"""

from __future__ import annotations

from ..config import Settings
from ..types import ProviderTier
from .router import ProviderRouter
from .templating import parse_json_setting
from .vendors import ollama
from .vendors.anthropic_sdk import AnthropicLLM
from .vendors.bedrock import BedrockLLM
from .vendors.comfyui import ComfyUIImage, ComfyUIVideo
from .vendors.edge import EdgeTTSVoice
from .vendors.fal import FalImage, FalVideo
from .vendors.ffmpeg_motion import FFmpegMotionVideo
from .vendors.hashing import HashingEmbedding
from .vendors.local_voice import EspeakVoice, PiperVoice
from .vendors.openai_compat import (
    OpenAICompatEmbedding,
    OpenAICompatImage,
    OpenAICompatLLM,
    OpenAICompatVoice,
    make_endpoint,
    model_picker,
)
from .vendors.replicate import ReplicateImage, ReplicateVideo

# Free-tier OpenAI-compatible services. Models are discovered from each live /models list unless pinned.
FREE_PRESETS: tuple[tuple[str, str, str, str, object], ...] = (
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY", "GROQ_MODEL",
     model_picker(exclude=("whisper", "guard", "tts", "orpheus", "playai", "distil", "compound", "embed"),
                  prefer=("gpt-oss-120b", "70b", "versatile", "120b", "llama"))),
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY", "GEMINI_MODEL",
     model_picker(include=("gemini", "flash"), exclude=("lite", "image", "tts", "live", "audio", "embed", "thinking", "native"),
                  newest_first=True)),
    ("openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", "OPENROUTER_MODEL",
     model_picker(include=(":free",), exclude=("1.7b", "code", "nano"),  # never a paid model unless OPENROUTER_MODEL says so
                  prefer=("gemma-4-31b", "ultra", "super", "gemma", "nemotron"))),
)  # fmt: skip

_GATEWAY_TIERS = {"local": ProviderTier.LOCAL, "free": ProviderTier.FREE, "cloud": ProviderTier.CLOUD_PRIMARY}


def _secret(value) -> str | None:  # noqa: ANN001
    return value.get_secret_value() if value is not None else None


async def build_router(settings: Settings) -> ProviderRouter:
    router = ProviderRouter(concurrency_per_provider=settings.provider_concurrency)

    # ---- endpoints ---------------------------------------------------------
    ollama_url = settings.ollama_base_url
    if not ollama_url and settings.ollama_autodetect and await ollama.detect():
        ollama_url = ollama.DEFAULT_URL
    gateway = make_endpoint(
        label="gateway", base_url=settings.gateway_base_url, api_key=_secret(settings.gateway_api_key),
        requires_key=False, key_hint="set SAX_GATEWAY_BASE_URL",
    )  # fmt: skip
    gateway_tier = _GATEWAY_TIERS[settings.gateway_tier]
    openai = make_endpoint(
        label="openai", base_url=settings.openai_base_url, api_key=_secret(settings.openai_api_key),
        requires_key=True, key_hint="set OPENAI_API_KEY",
    )  # fmt: skip

    # ---- LLM: local → gateway → primary cloud → secondary cloud -------------
    router.register(ollama.OllamaLLM(ollama_url, settings.ollama_model))
    router.register(
        OpenAICompatLLM(gateway, name="gateway", model=settings.gateway_model, tier=gateway_tier, model_hint="SAX_GATEWAY_MODEL")
    )
    for name, base_url, key_env, model_env, picker in FREE_PRESETS:
        endpoint = make_endpoint(label=name, base_url=base_url, api_key=_secret(getattr(settings, f"{name}_api_key")),
                                 requires_key=True, key_hint=f"set {key_env}")  # fmt: skip
        router.register(OpenAICompatLLM(endpoint, name=name, model=getattr(settings, f"{name}_model"), tier=ProviderTier.FREE,
                                        model_hint=model_env, pick_model=picker))  # type: ignore[arg-type]  # fmt: skip
    router.register(
        AnthropicLLM(
            _secret(settings.anthropic_api_key), settings.anthropic_model,
            effort=settings.anthropic_effort, refusal_fallback=settings.anthropic_refusal_fallback,
        )  # fmt: skip
    )
    router.register(
        OpenAICompatLLM(openai, name="openai", model=settings.openai_model, tier=ProviderTier.CLOUD_PRIMARY, model_hint="OPENAI_MODEL")
    )
    router.register(
        BedrockLLM(
            _secret(settings.aws_access_key_id), _secret(settings.aws_secret_access_key),
            settings.aws_region, settings.bedrock_model_id,
        )  # fmt: skip
    )

    # ---- embeddings: semantic local first, lexical local always works -------
    router.register(ollama.OllamaEmbedding(ollama_url, settings.ollama_embed_model))
    router.register(HashingEmbedding())
    router.register(
        OpenAICompatEmbedding(gateway, name="gateway-embed", model=settings.gateway_embed_model, tier=gateway_tier, model_hint="SAX_GATEWAY_EMBED_MODEL")
    )
    router.register(
        OpenAICompatEmbedding(openai, name="openai-embed", model=settings.openai_embed_model, tier=ProviderTier.CLOUD_PRIMARY, model_hint="OPENAI_EMBED_MODEL")
    )

    # ---- images --------------------------------------------------------------
    router.register(ComfyUIImage(settings.comfyui_base_url, settings.comfyui_image_workflow))
    router.register(
        OpenAICompatImage(gateway, name="gateway-image", model=settings.gateway_image_model, tier=gateway_tier, model_hint="SAX_GATEWAY_IMAGE_MODEL")
    )
    router.register(
        ReplicateImage(
            _secret(settings.replicate_api_token), settings.replicate_image_model,
            parse_json_setting(settings.replicate_image_input, "REPLICATE_IMAGE_INPUT"), settings.replicate_api_base,
        )  # fmt: skip
    )
    router.register(
        FalImage(
            _secret(settings.fal_key), settings.fal_image_model,
            parse_json_setting(settings.fal_image_input, "FAL_IMAGE_INPUT"), settings.fal_queue_base,
        )  # fmt: skip
    )
    router.register(
        OpenAICompatImage(openai, name="openai-image", model=settings.openai_image_model, tier=ProviderTier.CLOUD_SECONDARY, model_hint="OPENAI_IMAGE_MODEL")
    )

    # ---- video: generative providers first, camera-motion fallback last ------
    router.register(ComfyUIVideo(settings.comfyui_base_url, settings.comfyui_video_workflow))
    router.register(
        ReplicateVideo(
            _secret(settings.replicate_api_token), settings.replicate_video_model, settings.replicate_video_image_key,
            parse_json_setting(settings.replicate_video_input, "REPLICATE_VIDEO_INPUT"), settings.replicate_api_base,
        )  # fmt: skip
    )
    router.register(
        FalVideo(
            _secret(settings.fal_key), settings.fal_video_model, settings.fal_video_image_key,
            parse_json_setting(settings.fal_video_input, "FAL_VIDEO_INPUT"), settings.fal_queue_base,
        )  # fmt: skip
    )
    router.register(FFmpegMotionVideo())

    # ---- voice: neural local → free online → cloud → robotic local fallback --
    router.register(PiperVoice(settings.piper_bin, settings.piper_model))
    router.register(EdgeTTSVoice(settings.edge_tts_enabled))
    router.register(
        OpenAICompatVoice(
            gateway, name="gateway-voice", model=settings.gateway_tts_model, voice=settings.gateway_tts_voice,
            tier=gateway_tier, model_hint="SAX_GATEWAY_TTS_MODEL",
        )  # fmt: skip
    )
    router.register(
        OpenAICompatVoice(
            openai, name="openai-voice", model=settings.openai_tts_model, voice=settings.openai_tts_voice,
            tier=ProviderTier.CLOUD_PRIMARY, model_hint="OPENAI_TTS_MODEL",
        )  # fmt: skip
    )
    router.register(EspeakVoice(settings.espeak_bin))
    return router
