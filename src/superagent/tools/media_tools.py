"""Media tools: thin, validated wrappers over the provider router and FFmpeg."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..errors import CommandBlockedError, ConfigError
from ..lang import speech_language
from ..media.probe import probe, probe_image
from ..proc import run_process, scrubbed_env
from ..providers.base import ImageRequest, MotionPlan, VideoRequest, VoiceRequest
from ..types import PermissionLevel, RiskLevel
from ..utils import new_id
from .registry import ApprovalNeed, ToolContext, ToolRegistry, ToolSpec


def _ai(ctx: ToolContext):  # noqa: ANN202
    if ctx.ai is None:
        raise ConfigError("no provider router is attached to this tool context")
    return ctx.ai.scoped(execution_id=ctx.execution_id, task_id=ctx.task_id, project_id=ctx.project_id)


def _out_dir(ctx: ToolContext, requested: str | None, default: str) -> Path:
    return ctx.sandbox.resolve(requested or f"outputs/{default}", write=True)


class ImageGenInput(BaseModel):
    prompt: str = Field(min_length=1)
    negative_prompt: str = ""
    width: int = Field(1024, ge=64, le=4096)
    height: int = Field(1024, ge=64, le=4096)
    seed: int | None = None
    num_images: int = Field(1, ge=1, le=8)
    reference_images: list[str] = Field(default_factory=list)
    out_dir: str | None = None
    provider: str | None = None


async def image_generate(args: ImageGenInput, ctx: ToolContext) -> dict[str, Any]:
    out_dir = _out_dir(ctx, args.out_dir, "images")
    request = ImageRequest(
        prompt=args.prompt, negative_prompt=args.negative_prompt, width=args.width, height=args.height,
        seed=args.seed, num_images=args.num_images,
        reference_images=[ctx.sandbox.resolve(p) for p in args.reference_images],
    )  # fmt: skip
    results = await _ai(ctx).image(request, out_dir, only=args.provider)
    return {
        "paths": [str(r.path) for r in results], "provider": results[0].provider, "model": results[0].model,
        "width": args.width, "height": args.height, "count": len(results),
    }  # fmt: skip


class VideoGenInput(BaseModel):
    prompt: str = ""
    image: str | None = None
    duration: float = Field(5.0, gt=0, le=60)
    fps: int = Field(30, ge=8, le=60)
    width: int = Field(1080, ge=64, le=3840)
    height: int = Field(1920, ge=64, le=3840)
    camera: str = "push-in"
    intensity: float = Field(0.5, ge=0, le=1)
    seed: int | None = None
    out_path: str | None = None
    provider: str | None = None


async def video_generate(args: VideoGenInput, ctx: ToolContext) -> dict[str, Any]:
    out_path = ctx.sandbox.resolve(args.out_path or f"outputs/videos/{new_id('clip')}.mp4", write=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image = ctx.sandbox.resolve(args.image) if args.image else None
    request = VideoRequest(
        prompt=args.prompt, image=image, duration=args.duration, fps=args.fps, width=args.width, height=args.height,
        seed=args.seed, motion=MotionPlan(camera=args.camera, intensity=args.intensity),
    )  # fmt: skip
    operation = "image_to_video" if image is not None else "text_to_video"
    result = await _ai(ctx).video(request, out_path, operation=operation, only=args.provider)
    info = await probe(result.path)
    return {
        "path": str(result.path), "provider": result.provider, "model": result.model, "generative": result.generative,
        "duration": info.duration, "notes": result.metadata.get("notes", []),
    }  # fmt: skip


class VoiceGenInput(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    voice: str | None = None
    language: str | None = None
    speed: float = Field(1.0, gt=0.25, le=3)
    out_path: str | None = None
    provider: str | None = None


async def voice_generate(args: VoiceGenInput, ctx: ToolContext) -> dict[str, Any]:
    out_path = ctx.sandbox.resolve(args.out_path or f"outputs/audio/{new_id('voice')}.wav", write=True)
    request = VoiceRequest(text=args.text, voice=args.voice, language=speech_language(args.text, args.language), speed=args.speed)
    result = await _ai(ctx).speak(request, out_path, only=args.provider)
    info = await probe(result.path)
    return {"path": str(result.path), "provider": result.provider, "voice": result.voice, "duration": info.duration}


class ProbeInput(BaseModel):
    path: str


async def ffprobe_inspect(args: ProbeInput, ctx: ToolContext) -> dict[str, Any]:
    path = ctx.sandbox.resolve(args.path)
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
        return {"kind": "image", **probe_image(path).model_dump()}
    info = await probe(path)
    return {"kind": "video" if info.has_video else "audio", **info.model_dump()}


class FfmpegInput(BaseModel):
    args: list[str] = Field(min_length=1)
    timeout: float = Field(600.0, gt=0, le=7200)


def _ffmpeg_guard(args: FfmpegInput, ctx: ToolContext) -> ApprovalNeed | None:
    decision = ctx.policy.check(["ffmpeg", *args.args], ctx.sandbox.primary)
    if decision.action == "deny":
        raise CommandBlockedError(f"ffmpeg arguments blocked: {decision.reason}")
    if decision.action == "approve":
        return ApprovalNeed(decision.category or "network", decision.reason, {"args": args.args})
    return None


async def ffmpeg_render(args: FfmpegInput, ctx: ToolContext) -> dict[str, Any]:
    result = await run_process(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", *args.args], timeout=args.timeout,
        cwd=ctx.sandbox.primary, env=scrubbed_env(ctx.sandbox.primary), raise_on_timeout=False,
    )  # fmt: skip
    return {"exit_code": result.returncode, "stderr": result.tail(2000), "duration": round(result.duration, 3)}


def register(registry: ToolRegistry) -> None:
    read, safe = PermissionLevel.READ_ONLY, PermissionLevel.SAFE_WRITE
    specs = [
        ToolSpec("image.generate", "Generate images through the configured image provider.", ImageGenInput, image_generate, safe, RiskLevel.MEDIUM, 1900),
        ToolSpec("video.generate", "Generate a clip from text or animate a still image.", VideoGenInput, video_generate, safe, RiskLevel.MEDIUM, 3700),
        ToolSpec("voice.generate", "Synthesize speech from text.", VoiceGenInput, voice_generate, safe, RiskLevel.LOW, 700),
        ToolSpec("audio.generate", "Synthesize narration audio (alias of voice.generate).", VoiceGenInput, voice_generate, safe, RiskLevel.LOW, 700),
        ToolSpec("ffprobe.inspect", "Inspect an image, audio or video file.", ProbeInput, ffprobe_inspect, read, RiskLevel.LOW, 90),
        ToolSpec("ffmpeg.render", "Run FFmpeg with explicit arguments inside the workspace.", FfmpegInput, ffmpeg_render, safe, RiskLevel.MEDIUM, 7300, guard=_ffmpeg_guard),
    ]  # fmt: skip
    for spec in specs:
        registry.register(spec)
