"""Creative agents. Scene work is spawned as a parallel task graph by the director's storyboard step."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..agents.base import AgentContext, AgentResult, BaseAgent, action
from ..core.dag import Task
from ..errors import ApprovalDenied, ValidationFailed
from ..lang import speech_language
from ..media.ffmpeg import ffmpeg
from ..media.probe import probe
from ..providers.base import ImageRequest, VoiceRequest
from ..types import PermissionLevel, RiskLevel
from ..utils import slugify, truncate_middle
from . import editor, qc, subtitles, thumbnail
from .generation import GenerationEngine
from .models import ASPECT_FLOAT, QUALITY, CreativeBrief, ProjectManifest
from .project import ProjectStore
from .story import StoryEngine, brief_from_command, resolve_style


class CreativeServices:
    def __init__(self, platform: Any):
        self.platform = platform
        self.store = ProjectStore(platform.settings, platform.repo)
        self.generation = GenerationEngine(platform.repo, self.store)


def svc(ctx: AgentContext) -> CreativeServices:
    return ctx.services.creative_services


class ProjectInput(BaseModel):
    project_id: str


class SceneInput(ProjectInput):
    scene_id: str
    force: bool = False


def _task(ctx: AgentContext, agent: str, act: str, desc: str, inputs: dict[str, Any], deps: list[str], **kw: Any) -> Task:
    s = ctx.settings
    return Task(description=desc, agent=agent, action=act, input=inputs, dependencies=deps, parent_id=ctx.task.id,
                max_retries=kw.pop("max_retries", s.max_retries), timeout=kw.pop("timeout", s.task_timeout),
                metadata={"project_id": ctx.project_id or inputs.get("project_id"), **kw.pop("metadata", {})}, **kw)  # fmt: skip


def _seed_bump(ctx: AgentContext) -> int:
    return int(ctx.recovery.get("seed_bump", 0))


# =============================================================== director
class BriefInput(BaseModel):
    idea: str = Field(min_length=3)
    title: str = ""
    language: str | None = None
    duration_seconds: float | None = None
    aspect_ratio: str | None = None
    style: str | None = None
    tone: str | None = None
    quality: str | None = None
    ambient_music: bool = False
    music_path: str | None = None
    subtitles: bool = True


class StoryboardInput(ProjectInput):
    spawn_pipeline: bool = True
    export: bool = True


class CreativeDirectorAgent(BaseAgent):
    name = "director"
    description = "Turns an idea into a creative brief, story, character and style bibles, shot list and the parallel production plan."
    capabilities = ["creative.brief", "creative.story", "creative.storyboard"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("brief", description="Create a project and creative brief from an idea (infers format, duration and style).", input=BriefInput)
    async def brief(self, ctx: AgentContext, args: BriefInput) -> AgentResult:
        from ..core.intent import extract_entities

        overrides = {k: v for k, v in {"title": args.title, "language": args.language, "duration_seconds": args.duration_seconds,
                                       "aspect_ratio": args.aspect_ratio, "style": args.style, "tone": args.tone, "quality": args.quality or ctx.settings.creative_quality,
                                       "music_path": args.music_path}.items() if v}  # fmt: skip
        overrides.update(ambient_music=args.ambient_music, subtitles=args.subtitles)
        brief = brief_from_command(args.idea, extract_entities(args.idea), ctx.language if ctx.language != "hi" else "hi", **overrides)
        manifest = await svc(ctx).store.create(brief)
        style_key, style = resolve_style(brief.style)
        manifest.style = style
        manifest.style.aspect_ratio = brief.aspect_ratio
        svc(ctx).store.save_manifest(manifest)
        ctx.task.metadata["project_id"] = manifest.project_id
        w, h = brief.size
        return AgentResult(
            output={"project_id": manifest.project_id, "brief": brief.model_dump(mode="json"), "style": style_key, "size": [w, h],
                    "root": str(svc(ctx).store.root(manifest.project_id))},
            summary=f"project {manifest.project_id}: {brief.duration_seconds:g}s {brief.aspect_ratio} {style_key} in {brief.language}",
            artifacts=[str(svc(ctx).store.manifest_path(manifest.project_id))],
            verify=[{"type": "file_exists", "key": "root", "min_bytes": 0}] if False else [{"type": "schema", "required": ["project_id", "brief"]}],
        )  # fmt: skip

    @action("storyboard", description="Write the story, character and style bibles and shot list; spawn the per-scene production tasks.",
            input=StoryboardInput, needs_llm=False)  # fmt: skip
    async def storyboard(self, ctx: AgentContext, args: StoryboardInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        engine = StoryEngine(ctx.ai)
        story = await engine.create(manifest.brief, manifest.style)
        warnings = []
        if story.method == "template":
            warnings.append("no language model configured: the story is a generic three-act template (structure, not writing)")

        def mutate(m: ProjectManifest) -> None:
            m.story, m.status = story, "STORYBOARDED"

        manifest = await s.store.update(args.project_id, mutate)
        s.store.dump_json(args.project_id, "script", "story.json", story.model_dump(mode="json"))
        s.store.dump_json(args.project_id, "storyboard", "shot_list.json", [
            {"scene_id": sc.scene_id, "duration": sc.duration, "camera": sc.camera, "lens": sc.lens, "image_prompt": sc.image_prompt,
             "video_prompt": sc.video_prompt, "narration": sc.narration} for sc in story.scenes])  # fmt: skip
        s.store.dump_json(args.project_id, "storyboard", "style_bible.json", manifest.style.model_dump(mode="json"))
        for character in story.characters:
            s.store.dump_json(args.project_id, "characters", f"{character.character_id}.json", character.model_dump(mode="json"))

        spawned: list[Task] = []
        render_id = None
        if args.spawn_pipeline:
            pid = args.project_id
            img_ids, vid_ids, voc_ids = [], [], []
            for scene in story.scenes:
                img = _task(ctx, "imager", "scene", f"image for {scene.scene_id}", {"project_id": pid, "scene_id": scene.scene_id}, [ctx.task.id])
                voc = _task(ctx, "voice", "scene", f"narration for {scene.scene_id}", {"project_id": pid, "scene_id": scene.scene_id}, [ctx.task.id])
                vid = _task(ctx, "videographer", "scene", f"clip for {scene.scene_id}",
                            {"project_id": pid, "scene_id": scene.scene_id, "image": f"${{{img.id}}}", "voice": f"${{{voc.id}}}"}, [img.id, voc.id])  # fmt: skip
                spawned += [img, voc, vid]
                img_ids.append(img.id)
                voc_ids.append(voc.id)
                vid_ids.append(vid.id)
            videos = [f"${{{i}}}" for i in vid_ids]
            voices = [f"${{{i}}}" for i in voc_ids]
            sub = None
            if manifest.brief.subtitles:
                sub = _task(ctx, "subtitles", "generate", "subtitles from narration timing", {"project_id": pid, "videos": videos, "voices": voices}, [*vid_ids, *voc_ids])  # fmt: skip
                spawned.append(sub)
            render = _task(ctx, "editor", "render", "assemble, mix and render the final video",
                           {"project_id": pid, "videos": videos, "voices": voices, **({"subtitles": f"${{{sub.id}}}"} if sub else {})},
                           [*vid_ids, *voc_ids, *([sub.id] if sub else [])], timeout=max(1800.0, ctx.settings.task_timeout))  # fmt: skip
            render_id = render.id
            thumb = _task(ctx, "thumbnail", "create", "thumbnail and cover", {"project_id": pid}, [*img_ids], metadata={"optional": True}, max_retries=1)  # fmt: skip
            social = _task(ctx, "content", "social", "titles, description, caption and hashtags", {"project_id": pid}, [ctx.task.id], metadata={"optional": True}, max_retries=1)  # fmt: skip
            final = _task(ctx, "quality", "final", "final quality gates", {"project_id": pid, "render": f"${{{render.id}}}"}, [render.id])
            spawned += [render, thumb, social, final]
            if args.export:
                spawned.append(_task(ctx, "editor", "export", "export package (needs approval)",
                                     {"project_id": pid, "qc": f"${{{final.id}}}", "render": f"${{{render.id}}}"},
                                     [final.id, render.id, thumb.id, social.id], priority=8))  # fmt: skip
        out = {"project_id": args.project_id, "title": story.title, "scenes": len(story.scenes), "duration": story.duration,
               "method": story.method, "characters": [c.name for c in story.characters], "render_task": render_id,
               "text": f"{story.title}: {len(story.scenes)} scenes, {story.duration:g}s"}  # fmt: skip
        return AgentResult(output=out, summary=out["text"], warnings=warnings, spawn=spawned, artifacts=[str(s.store.path(args.project_id, "script", "story.json"))],
                           verify=[{"type": "list_min", "key": "scenes", "min": 1}, {"type": "non_empty", "key": "title"}])  # fmt: skip


# ================================================================== imager
class FreeImageInput(BaseModel):
    prompt: str = Field(min_length=2)
    width: int = Field(1024, ge=64, le=4096)
    height: int = Field(1024, ge=64, le=4096)
    count: int = Field(1, ge=1, le=6)
    negative_prompt: str = ""
    out_dir: str | None = None


class ImageAgent(BaseAgent):
    name = "imager"
    description = "Generates images through the provider router with variations, QC scoring, best-take selection, caching and resume."
    capabilities = ["image.generate", "image.scene", "image.character_sheet"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("scene", description="Generate (best of N) the image for one scene of a project.", input=SceneInput)
    async def scene(self, ctx: AgentContext, args: SceneInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        scene = manifest.scene(args.scene_id)
        result = await s.generation.scene_image(ctx.ai, manifest, scene, execution_id=ctx.execution_id, force=args.force,
                                                seed_bump=_seed_bump(ctx), only=ctx.recovery.get("only_provider"))  # fmt: skip
        w, h = manifest.brief.size
        warnings = [] if result.get("cached") else list((result.get("qc") or {}).get("warnings", []))
        if (result.get("qc") or {}).get("not_assessed"):
            warnings.append(f"{args.scene_id}: not assessed - " + ", ".join(result["qc"]["not_assessed"]))
        return AgentResult(output={**result, "scene_id": args.scene_id, "project_id": args.project_id},
                           summary=f"{args.scene_id}: {result['provider']} score {result['score']}" + (" (cached)" if result.get("cached") else ""),
                           artifacts=[result["path"]], warnings=list(dict.fromkeys(warnings)),
                           verify=[{"type": "image", "key": "path", "width": w, "height": h}])  # fmt: skip

    @action("create", description="Generate images from a free prompt (not tied to a project).", input=FreeImageInput)
    async def create(self, ctx: AgentContext, args: FreeImageInput) -> AgentResult:
        out_dir = Path(args.out_dir) if args.out_dir else ctx.out_dir("images")
        out_dir = ctx.tools.context.sandbox.resolve(out_dir, write=True)
        results = await ctx.ai.image(ImageRequest(prompt=args.prompt, negative_prompt=args.negative_prompt, width=args.width,
                                                  height=args.height, num_images=args.count, seed=_seed_bump(ctx) or None), out_dir)  # fmt: skip
        scored: list[dict[str, Any]] = []
        for r in results:
            q = await qc.qc_image_full(ctx.ai, r.path, args.prompt, width=args.width, height=args.height, review=False)
            scored.append({"path": str(r.path), "score": round(q.score, 4), "ok": q.ok, "problems": q.problems})
        best = max(scored, key=lambda x: x["score"])
        if not best["ok"]:
            from ..errors import MediaError

            raise MediaError("generated images failed QC: " + "; ".join(best["problems"]))
        return AgentResult(output={"paths": [x["path"] for x in scored], "best": best["path"], "images": scored, "provider": results[0].provider},
                           summary=f"{len(results)} image(s) via {results[0].provider}", artifacts=[x["path"] for x in scored],
                           verify=[{"type": "image", "key": "best", "width": args.width, "height": args.height}])  # fmt: skip

    @action("character_sheet", description="Generate a reference image for each character (conditions later scenes where supported).", input=ProjectInput)
    async def character_sheet(self, ctx: AgentContext, args: ProjectInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        made = []
        for character in manifest.story.characters if manifest.story else []:
            prompt = f"character reference sheet, neutral background, front view: {character.prompt_fragment()}. {manifest.style.prompt_fragment()}"
            results = await ctx.ai.image(ImageRequest(prompt=prompt, negative_prompt=manifest.style.negative, width=1024, height=1024,
                                                      seed=character.seed), s.store.path(args.project_id, "characters", "x.keep").parent)  # fmt: skip
            target = s.store.path(args.project_id, "characters", f"{character.character_id}.png")
            shutil.copy2(results[0].path, target)
            character.reference_assets = [str(target)]
            made.append(str(target))

        def mutate(m: ProjectManifest) -> None:
            m.story = manifest.story

        await s.store.update(args.project_id, mutate)
        return AgentResult(output={"sheets": made}, summary=f"{len(made)} character sheet(s)", artifacts=made,
                           verify=[{"type": "file_exists", "key": "sheets"}] if made else [])  # fmt: skip


# ============================================================ videographer
class SceneVideoInput(SceneInput):
    image: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None


class FreeVideoInput(BaseModel):
    prompt: str = ""
    image: str | None = None
    duration: float = Field(5.0, gt=0, le=60)
    width: int = 1080
    height: int = 1920
    camera: str = "push-in"
    out_path: str | None = None


class VideoAgent(BaseAgent):
    name = "videographer"
    description = "Produces scene clips (image-to-video or text-to-video) with QC, best-take selection, caching and resume."
    capabilities = ["video.generate", "video.scene"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("scene", description="Make the clip for one scene from its image and narration length.", input=SceneVideoInput)
    async def scene(self, ctx: AgentContext, args: SceneVideoInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        scene = manifest.scene(args.scene_id)
        image = Path(args.image["path"]) if args.image and args.image.get("path") else None
        voice_duration = float((args.voice or {}).get("duration") or 0.0)
        duration = max(scene.duration, voice_duration + editor_pad())
        result = await s.generation.scene_video(ctx.ai, manifest, scene, image, duration=duration, execution_id=ctx.execution_id, force=args.force,
                                                seed_bump=_seed_bump(ctx), only=ctx.recovery.get("only_provider"))  # fmt: skip
        warnings = list(result["notes"]) if not result.get("generative", True) else []
        warnings += (result.get("qc") or {}).get("warnings", [])
        w, h = manifest.brief.size
        return AgentResult(output={**result, "scene_id": args.scene_id, "project_id": args.project_id},
                           summary=f"{args.scene_id}: {duration:.1f}s via {result['provider']}" + ("" if result.get("generative", True) else " (camera motion only)"),
                           artifacts=[result["path"]], warnings=list(dict.fromkeys(warnings)),
                           verify=[{"type": "media", "key": "path", "kind": "video", "width": w, "height": h, "min_duration": min(duration * 0.5, 1.0)}])  # fmt: skip

    @action("create", description="Make a clip from a prompt and/or an image (not tied to a project).", input=FreeVideoInput)
    async def create(self, ctx: AgentContext, args: FreeVideoInput) -> AgentResult:
        data = await ctx.tools.call("video.generate", prompt=args.prompt, image=args.image, duration=args.duration, width=args.width,
                                    height=args.height, camera=args.camera, out_path=args.out_path)  # fmt: skip
        return AgentResult(output=data, summary=f"{data['duration']:.1f}s clip via {data['provider']}", artifacts=[data["path"]],
                           warnings=data.get("notes", []), verify=[{"type": "media", "key": "path", "kind": "video"}])  # fmt: skip


def editor_pad() -> float:
    return 0.25


# ================================================================== voice
class SpeakInput(BaseModel):
    text: str = Field(min_length=1)
    language: str | None = None
    out_path: str | None = None


class VoiceAgent(BaseAgent):
    name = "voice"
    description = "Speaks narration and dialogue, giving each recurring character a consistent distinct voice."
    capabilities = ["voice.narrate", "voice.dialogue"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("scene", description="Synthesize one scene's narration and dialogue into a single audio file.", input=SceneInput)
    async def scene(self, ctx: AgentContext, args: SceneInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        scene = manifest.scene(args.scene_id)
        language = speech_language(" ".join([scene.narration, *[d.text for d in scene.dialogue]]), manifest.brief.language)
        lines: list[tuple[int, str]] = [(0, scene.narration)] if scene.narration.strip() else []
        order = {c.character_id: i + 1 for i, c in enumerate(manifest.story.characters if manifest.story else [])}
        names = {c.name: c.character_id for c in (manifest.story.characters if manifest.story else [])}
        lines += [(order.get(d.speaker, order.get(names.get(d.speaker, ""), 1)), d.text) for d in scene.dialogue if d.text.strip()]
        target = s.store.path(args.project_id, "audio", f"{scene.scene_id}.wav")
        if not lines:
            await ffmpeg(["-f", "lavfi", "-t", "0.5", "-i", "anullsrc=r=48000:cl=mono", "-c:a", "pcm_s16le", target])
            return AgentResult(output={"path": str(target), "duration": 0.5, "scene_id": scene.scene_id, "silent": True},
                               summary=f"{scene.scene_id}: no narration", artifacts=[str(target)], verify=[{"type": "media", "key": "path", "kind": "audio"}])  # fmt: skip
        fingerprint = hashlib.sha256(json.dumps([lines, language], ensure_ascii=False).encode()).hexdigest()[:16]
        sidecar = target.with_suffix(".voice.json")
        if target.is_file() and sidecar.is_file() and json.loads(sidecar.read_text()).get("fingerprint") == fingerprint:
            info = await probe(target)
            return AgentResult(output={"path": str(target), "duration": info.duration, "scene_id": scene.scene_id, "cached": True},
                               summary=f"{scene.scene_id}: voice {info.duration:.1f}s (cached)", artifacts=[str(target)],
                               verify=[{"type": "media", "key": "path", "kind": "audio"}])  # fmt: skip
        parts: list[Path] = []
        provider = voice_id = None
        for index, (variant, text) in enumerate(lines):
            part = target.with_name(f"{scene.scene_id}.part{index}.wav")
            spoken = await ctx.ai.speak(VoiceRequest(text=text, language=language, variant=variant), part, only=ctx.recovery.get("only_provider"))
            parts.append(spoken.path)
            provider, voice_id = spoken.provider, spoken.voice
        inputs: list[str | Path] = []
        for p in parts:
            inputs += ["-i", p]
        graph = "".join(f"[{i}:a]aresample=48000,aformat=channel_layouts=mono,apad=pad_dur=0.2[a{i}];" for i in range(len(parts)))
        graph += "".join(f"[a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1[out]"
        await ffmpeg([*inputs, "-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s16le", target], timeout=300)
        for p in parts:
            p.unlink(missing_ok=True)
        sidecar.write_text(json.dumps({"fingerprint": fingerprint}))
        info = await probe(target)
        return AgentResult(output={"path": str(target), "duration": info.duration, "scene_id": scene.scene_id, "provider": provider, "voice": voice_id},
                           summary=f"{scene.scene_id}: voice {info.duration:.1f}s via {provider}", artifacts=[str(target)],
                           verify=[{"type": "media", "key": "path", "kind": "audio", "min_duration": 0.3}])  # fmt: skip

    @action("speak", description="Speak a block of text (not tied to a project).", input=SpeakInput)
    async def speak(self, ctx: AgentContext, args: SpeakInput) -> AgentResult:
        data = await ctx.tools.call("voice.generate", text=args.text, language=args.language, out_path=args.out_path)
        return AgentResult(output=data, summary=f"{data['duration']:.1f}s via {data['provider']}", artifacts=[data["path"]],
                           verify=[{"type": "media", "key": "path", "kind": "audio"}])  # fmt: skip


# ================================================================== audio
class AmbientInput(ProjectInput):
    duration: float = Field(30.0, gt=1, le=900)


class LevelsInput(BaseModel):
    path: str


class AudioAgent(BaseAgent):
    name = "audio"
    description = "Creates an original procedural ambient bed and measures audio levels. Never uses copyrighted music on its own."
    capabilities = ["audio.ambient", "audio.analyze"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("ambient", description="Generate an original procedural ambient bed for a project.", input=AmbientInput)
    async def ambient(self, ctx: AgentContext, args: AmbientInput) -> AgentResult:
        target = svc(ctx).store.path(args.project_id, "audio", "ambient_bed.wav")
        await editor.mix_ambient(target, args.duration)
        return AgentResult(output={"path": str(target), "duration": args.duration}, summary=f"{args.duration:g}s procedural ambient bed",
                           artifacts=[str(target)], verify=[{"type": "media", "key": "path", "kind": "audio"}])  # fmt: skip

    @action("analyze", description="Measure mean and peak level of an audio or video file.", input=LevelsInput)
    async def analyze(self, ctx: AgentContext, args: LevelsInput) -> AgentResult:
        levels = await qc.audio_levels(ctx.tools.context.sandbox.resolve(args.path))
        return AgentResult(output=levels, summary=f"mean {levels['mean_db']} dB, peak {levels['max_db']} dB")


# ============================================================== subtitles
class SubtitleInput(ProjectInput):
    videos: list[dict[str, Any]]
    voices: list[dict[str, Any]]
    transition: float = editor.TRANSITION_SECONDS


def _align(project: ProjectManifest, videos: list[dict[str, Any]], voices: list[dict[str, Any]]) -> list[tuple[Any, dict[str, Any], dict[str, Any]]]:
    by_video = {v["scene_id"]: v for v in videos}
    by_voice = {v["scene_id"]: v for v in voices}
    missing = [sc.scene_id for sc in project.story.scenes if sc.scene_id not in by_video or sc.scene_id not in by_voice]  # type: ignore[union-attr]
    if missing:
        raise ValidationFailed(f"scenes without a clip or voice: {', '.join(missing)}")
    return [(sc, by_video[sc.scene_id], by_voice[sc.scene_id]) for sc in project.story.scenes]  # type: ignore[union-attr]


class SubtitleAgent(BaseAgent):
    name = "subtitles"
    description = "Builds SRT, VTT and ASS subtitles timed to the real narration audio (Hindi, Hinglish, English)."
    capabilities = ["subtitles.generate"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("generate", description="Create subtitle files from scene narration and measured voice durations.", input=SubtitleInput)
    async def generate(self, ctx: AgentContext, args: SubtitleInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        rows = _align(manifest, args.videos, args.voices)
        durations, voice_durations = [], []
        for _, video, voice in rows:
            v, a = (await probe(Path(video["path"]))).duration, (await probe(Path(voice["path"]))).duration
            durations.append(max(v, a + editor_pad()))
            voice_durations.append(a)
        starts = editor.scene_starts(durations, args.transition)
        w, h = manifest.brief.size
        cues = subtitles.build_cues([(st, vd, " ".join([sc.narration, *[d.text for d in sc.dialogue]]))
                                     for st, vd, (sc, _, _) in zip(starts, voice_durations, rows, strict=True)],
                                    max_chars=22 if h > w else 40)  # fmt: skip
        if not cues:
            raise ValidationFailed("no narration text to subtitle")
        paths = subtitles.write_all(cues, s.store.path(args.project_id, "subtitles", "x.keep").parent, "captions", w, h)
        return AgentResult(output={"paths": paths, "ass": paths["ass"], "srt": paths["srt"], "cues": len(cues), "transition": args.transition},
                           summary=f"{len(cues)} cues in SRT/VTT/ASS", artifacts=list(paths.values()),
                           verify=[{"type": "file_exists", "key": "ass"}, {"type": "list_min", "key": "cues", "min": 1}])  # fmt: skip


# ================================================================== editor
class RenderInput(ProjectInput):
    videos: list[dict[str, Any]]
    voices: list[dict[str, Any]]
    subtitles: dict[str, Any] | None = None
    transition: float = editor.TRANSITION_SECONDS


class ExportInput(ProjectInput):
    qc: dict[str, Any]
    render: dict[str, Any]


class EditorAgent(BaseAgent):
    name = "editor"
    description = "Assembles scene clips and narration with FFmpeg: transitions, music ducking, loudness, subtitles, logo; exports approved packages."
    capabilities = ["video.edit", "video.render", "video.export"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("render", description="Render the final video from scene clips and narration.", input=RenderInput)
    async def render(self, ctx: AgentContext, args: RenderInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        brief = manifest.brief
        w, h = brief.size
        rows = _align(manifest, args.videos, args.voices)
        work = s.store.path(args.project_id, "renders", "work", "x.keep").parent
        segments = await asyncio.gather(*(editor.build_segment(editor.SegmentSpec(sc.scene_id, Path(v["path"]), Path(a["path"]), sc.narration),
                                                               work / f"{sc.scene_id}.mp4", width=w, height=h, fps=brief.fps) for sc, v, a in rows))  # fmt: skip
        joined = work / "joined.mp4"
        total = await editor.join_segments(list(segments), joined, transition=args.transition, fps=brief.fps)
        music = None
        if brief.music_path:
            music = ctx.tools.context.sandbox.resolve(brief.music_path)
        elif brief.ambient_music:
            music = await editor.mix_ambient(s.store.path(args.project_id, "audio", "ambient_bed.wav"), total + 2)
        ass = Path(args.subtitles["ass"]) if args.subtitles and args.subtitles.get("ass") else None
        mark = ctx.tools.context.sandbox.resolve(brief.watermark_path) if brief.watermark_path else None
        final = s.store.path(args.project_id, "renders", f"{slugify(manifest.story.title if manifest.story else manifest.name)}.mp4")
        info = await editor.finalize(joined, final, width=w, height=h, duration=total, subtitles_ass=ass, music=music, watermark=mark, fps=brief.fps)
        shutil.rmtree(work, ignore_errors=True)
        expected = sum(sg.duration for sg in segments) - args.transition * (len(segments) - 1)

        def mutate(m: ProjectManifest) -> None:
            m.outputs["render"] = {"path": str(final), "duration": info.duration, "expected_duration": expected, "subtitles": bool(ass),
                                   "music": "user file" if brief.music_path else ("procedural ambient" if brief.ambient_music else "none")}  # fmt: skip
            m.status = "RENDERED"

        await s.store.update(args.project_id, mutate)
        await s.store.register_asset(args.project_id, "render", final, duration=info.duration)
        return AgentResult(output={"path": str(final), "duration": info.duration, "expected_duration": expected, "width": w, "height": h,
                                   "subtitles": bool(ass), "scenes": len(rows)}, summary=f"rendered {info.duration:.1f}s {w}x{h}: {final.name}",
                           artifacts=[str(final)],
                           verify=[{"type": "media", "key": "path", "kind": "video", "width": w, "height": h, "fps": brief.fps, "has_audio": True,
                                    "codec": "h264", "duration": expected, "duration_tolerance": 1.5}])  # fmt: skip

    @action("export", description="Copy the approved final video and its metadata into exports/. Always needs approval; it never uploads.", input=ExportInput)
    async def export(self, ctx: AgentContext, args: ExportInput) -> AgentResult:
        s = svc(ctx)
        if not args.qc.get("final_ready"):
            raise ValidationFailed("final quality gates did not pass; nothing to export")
        approvals = ctx.tools.context.approvals
        if approvals is None:
            raise ApprovalDenied("exporting needs an approval channel")
        manifest = s.store.load(args.project_id)
        await approvals.ensure(action="creative.export", category="export_publish", risk=RiskLevel.MEDIUM,
                               reason=f"export '{manifest.name}' as a finished deliverable (no upload is performed)",
                               details={"project_id": args.project_id, "render": Path(args.render["path"]).name},
                               execution_id=ctx.execution_id, task_id=ctx.task.id)  # fmt: skip
        out_dir = s.store.path(args.project_id, "exports", "x.keep").parent
        final = Path(args.render["path"])
        copied = out_dir / final.name
        shutil.copy2(final, copied)
        package: dict[str, Any] = {"video": copied.name, "project": manifest.name, "language": manifest.brief.language,
                                   "duration": args.render.get("duration"), "note": "export only: nothing was uploaded or published"}  # fmt: skip
        for key in ("social", "thumbnail"):
            if key in manifest.outputs:
                package[key] = manifest.outputs[key]
        meta = out_dir / "package.json"
        meta.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        for sub in ("subtitles",):
            for f in (s.store.root(args.project_id) / sub).glob("captions.*"):
                shutil.copy2(f, out_dir / f.name)

        def mutate(m: ProjectManifest) -> None:
            m.outputs["export"] = {"dir": str(out_dir), "video": str(copied)}
            m.status = "EXPORTED"

        await s.store.update(args.project_id, mutate)
        return AgentResult(output={"dir": str(out_dir), "video": str(copied), "package": str(meta)}, summary=f"exported to {out_dir}",
                           artifacts=[str(copied), str(meta)], verify=[{"type": "file_exists", "key": "video", "min_bytes": 1000}])  # fmt: skip


# ============================================================= thumbnail
class ThumbInput(ProjectInput):
    title: str | None = None


class ThumbnailAgent(BaseAgent):
    name = "thumbnail"
    description = "Builds YouTube, Shorts and Instagram thumbnails from the best scene image with legible titles (Hindi supported)."
    capabilities = ["thumbnail.create"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("create", description="Create thumbnails and covers and report their mobile legibility.", input=ThumbInput)
    async def create(self, ctx: AgentContext, args: ThumbInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        images = manifest.selected.get("image", {})
        if not images:
            raise ValidationFailed("the project has no scene images yet")
        gens = {g["scene_id"]: g["score"] or 0 for g in await ctx.repo.list_generations(args.project_id, kind="image") if g["selected"]}
        best_scene = max(images, key=lambda sid: gens.get(sid, 0))
        title = args.title or (manifest.story.title if manifest.story else manifest.name)
        made: dict[str, Any] = {}
        for key, size in (("youtube", thumbnail.SIZES["youtube"]), ("cover", thumbnail.SIZES["shorts"]), ("instagram", thumbnail.SIZES["instagram"])):
            made[key] = thumbnail.create(Path(images[best_scene]), title, s.store.path(args.project_id, "thumbnails", f"{key}.png"), size=size)
        warnings = [f"{k}: title may be hard to read on a phone" for k, v in made.items() if not v["mobile_readable"]]

        def mutate(m: ProjectManifest) -> None:
            m.outputs["thumbnail"] = {k: v["path"] for k, v in made.items()}

        await s.store.update(args.project_id, mutate)
        return AgentResult(output={"source_scene": best_scene, "thumbnails": made, "path": made["youtube"]["path"]},
                           summary=f"3 thumbnails from {best_scene}", artifacts=[v["path"] for v in made.values()], warnings=warnings,
                           verify=[{"type": "image", "key": "path", "width": 1280, "height": 720}])  # fmt: skip


# ================================================================= content
class SocialModel(BaseModel):
    title: str
    description: str
    caption: str
    hashtags: list[str]
    keywords: list[str]
    thumbnail_prompt: str = ""
    cover_prompt: str = ""


class ContentAgent(BaseAgent):
    name = "content"
    description = "Writes social metadata (title, description, caption, hashtags, keywords, thumbnail prompts). Prepares; never publishes."
    capabilities = ["content.social"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("social", description="Prepare YouTube/Instagram metadata for a project.", input=ProjectInput)
    async def social(self, ctx: AgentContext, args: ProjectInput) -> AgentResult:
        from ..data.documents import keywords as extract_keywords

        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        story = manifest.story
        title = story.title if story else manifest.name
        narration = " ".join(sc.narration for sc in story.scenes) if story else manifest.brief.idea
        warnings: list[str] = []
        if ctx.has_llm:
            lang = {"hi": "Hindi", "hinglish": "Hinglish", "en": "English"}.get(manifest.brief.language, "English")
            model = await ctx.ai.complete_json(
                f"Write social-media metadata in {lang} for this short video.\nTitle: {title}\nSynopsis: {story.synopsis if story else manifest.brief.idea}\n"
                f"Narration: {truncate_middle(narration, 1500)}\nTitle under 70 characters; description 2-3 sentences; 5-10 hashtags starting with #; "
                "no false claims.", SocialModel, max_tokens=1500, attempts=2)  # fmt: skip
            data = model.model_dump()
        else:
            words = extract_keywords(narration + " " + manifest.brief.idea, 8)
            data = {"title": title[:70], "description": (story.synopsis if story else manifest.brief.idea)[:300],
                    "caption": f"{title} #shorts", "hashtags": ["#" + slugify(w, 20).replace("-", "") for w in words[:6] if slugify(w)],
                    "keywords": words, "thumbnail_prompt": f"{title}, {manifest.style.prompt_fragment()}", "cover_prompt": f"{title}, vertical cover"}  # fmt: skip
            warnings.append("no language model configured: metadata is derived from the story text, not written")
        data["hashtags"] = [h if h.startswith("#") else "#" + h for h in data["hashtags"]][:12]
        data["publishing"] = "prepared only; publishing to YouTube or Instagram requires your approval and your own account"

        def mutate(m: ProjectManifest) -> None:
            m.outputs["social"] = data

        await s.store.update(args.project_id, mutate)
        path = s.store.dump_json(args.project_id, "metadata", "social.json", data)
        return AgentResult(output={**data, "path": str(path), "text": f"{data['title']}\n{data['caption']}\n{' '.join(data['hashtags'])}"},
                           summary=data["title"], artifacts=[str(path)], warnings=warnings,
                           verify=[{"type": "non_empty", "key": "title"}, {"type": "list_min", "key": "hashtags", "min": 1}])  # fmt: skip


# ================================================================= quality
class FinalInput(ProjectInput):
    render: dict[str, Any] | None = None


class QualityAgent(BaseAgent):
    name = "quality"
    description = "Runs the final quality gates: schema, technical, content, score, dependencies, render validation, approvals."
    capabilities = ["quality.final"]
    permissions = PermissionLevel.READ_ONLY

    @action("final", description="Evaluate every gate for a rendered project and decide whether it is final-ready.", input=FinalInput)
    async def final(self, ctx: AgentContext, args: FinalInput) -> AgentResult:
        s = svc(ctx)
        manifest = s.store.load(args.project_id)
        brief, story = manifest.brief, manifest.story
        w, h = brief.size
        render = (args.render or manifest.outputs.get("render") or {})
        scenes = story.scenes if story else []
        images, videos = manifest.selected.get("image", {}), manifest.selected.get("video", {})
        gates: dict[str, dict[str, Any]] = {}
        warnings: list[str] = []

        gates["1_schema"] = {"ok": bool(story and scenes and render.get("path")), "detail": "story, scenes and a render are recorded"}
        technical = await qc.qc_video(Path(render["path"]), width=w, height=h, fps=brief.fps,
                                      expected_duration=float(render.get("expected_duration") or story.duration if story else 1), duration_tolerance=1.5,
                                      require_audio=True) if render.get("path") and Path(render["path"]).is_file() else None  # fmt: skip
        gates["2_technical"] = {"ok": bool(technical and technical.ok), "detail": "; ".join(technical.problems) if technical else "no render file",
                                "metrics": technical.metrics if technical else {}}  # fmt: skip
        warnings += technical.warnings if technical else []
        narrated = [sc for sc in scenes if sc.narration.strip()]
        subs_ok = True
        if brief.subtitles:
            srt = s.store.root(args.project_id) / "subtitles" / "captions.srt"
            subs_ok = srt.is_file() and len(subtitles.parse_srt(srt.read_text(encoding="utf-8"))) >= len(narrated)
        gates["3_content"] = {"ok": bool(narrated) and subs_ok, "detail": f"{len(narrated)} narrated scene(s); subtitles {'ok' if subs_ok else 'missing or short'}"}
        rows = [g for g in await ctx.repo.list_generations(args.project_id) if g["selected"]]
        scores = [g["score"] for g in rows if g["score"] is not None]
        average = sum(scores) / len(scores) if scores else 0.0
        gates["4_quality_score"] = {"ok": bool(scores) and average >= 0.30 and min(scores) >= 0.15, "detail": f"mean {average:.2f}, min {min(scores) if scores else 0:.2f} over {len(scores)} selected takes"}  # fmt: skip
        gates["5_dependencies"] = {"ok": all(sc.scene_id in images and sc.scene_id in videos for sc in scenes),
                                   "detail": f"{sum(1 for sc in scenes if sc.scene_id in images and sc.scene_id in videos)}/{len(scenes)} scenes have image and clip"}  # fmt: skip
        levels = await qc.audio_levels(Path(render["path"])) if technical else {"mean_db": None, "max_db": None}
        audible = levels["mean_db"] is not None and levels["mean_db"] > -45
        gates["6_final_render"] = {"ok": bool(technical and technical.ok and audible), "detail": f"audio mean {levels['mean_db']} dB, peak {levels['max_db']} dB", "levels": levels}
        pending = [a for a in await ctx.repo.list_approvals(status="PENDING", execution_id=ctx.execution_id) if a["action"] != "creative.export"]
        gates["7_approval"] = {"ok": not pending, "detail": "export itself waits for your approval after these gates pass"}

        consistency: list[float] = []
        ordered = [Path(images[sc.scene_id]) for sc in scenes if sc.scene_id in images]
        from ..media.images import color_signature, histogram_similarity

        for a, b in zip(ordered, ordered[1:], strict=False):
            consistency.append(histogram_similarity(color_signature(a), color_signature(b)))
        if consistency and min(consistency) < 0.15:
            warnings.append("visual continuity: at least one scene changes palette sharply from the previous one")
        warnings.append("not assessed automatically: character likeness across scenes and in-frame artefacts (needs a vision model or your review)")
        failed = [name for name, gate in gates.items() if not gate["ok"]]
        out = {"final_ready": not failed, "gates": gates, "failed": failed, "mean_score": round(average, 3),
               "palette_consistency_min": round(min(consistency), 3) if consistency else None, "path": render.get("path", "")}  # fmt: skip

        def mutate(m: ProjectManifest) -> None:
            m.outputs["qc"] = {"final_ready": out["final_ready"], "failed": failed}

        await s.store.update(args.project_id, mutate)
        return AgentResult(output=out, summary="FINAL_READY" if not failed else "not final-ready: " + ", ".join(failed), warnings=warnings,
                           verify=[{"type": "equals", "key": "final_ready", "equals": True, "name": "all quality gates passed"}])  # fmt: skip


def all_agents() -> list[BaseAgent]:
    return [CreativeDirectorAgent(), ImageAgent(), VideoAgent(), VoiceAgent(), AudioAgent(), SubtitleAgent(), EditorAgent(), ThumbnailAgent(),
            ContentAgent(), QualityAgent()]  # fmt: skip


_ = (ASPECT_FLOAT, QUALITY, CreativeBrief)
