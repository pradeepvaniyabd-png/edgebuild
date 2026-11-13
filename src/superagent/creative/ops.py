"""Explicit task lists for project operations (used by the API and CLI instead of free-text planning)."""

from __future__ import annotations

from typing import Any

from ..config import Settings
from ..core.dag import Task
from ..errors import ValidationFailed
from .models import ProjectManifest


def _t(settings: Settings, project_id: str, agent: str, action: str, description: str, inputs: dict[str, Any], deps: list[str] | None = None, **kw: Any) -> Task:
    return Task(description=description, agent=agent, action=action, input={"project_id": project_id, **inputs}, dependencies=deps or [],
                max_retries=settings.max_retries, timeout=kw.pop("timeout", settings.task_timeout), metadata={"project_id": project_id}, **kw)  # fmt: skip


def _scenes(manifest: ProjectManifest, only: list[str] | None = None) -> list[str]:
    if manifest.story is None:
        raise ValidationFailed("the project has no storyboard yet", hint="create one first: super-agent storyboard / POST .../storyboard")
    ids = [s.scene_id for s in manifest.story.scenes]
    if only:
        unknown = [s for s in only if s not in ids]
        if unknown:
            raise ValidationFailed(f"unknown scene(s): {', '.join(unknown)}", hint="scenes: " + ", ".join(ids))
        return [s for s in ids if s in only]
    return ids


def storyboard_tasks(settings: Settings, project_id: str) -> list[Task]:
    return [_t(settings, project_id, "director", "storyboard", "write the story and shot list", {"spawn_pipeline": False})]


def image_tasks(settings: Settings, manifest: ProjectManifest, scenes: list[str] | None = None, force: bool = False) -> list[Task]:
    return [_t(settings, manifest.project_id, "imager", "scene", f"image for {sid}", {"scene_id": sid, "force": force}) for sid in _scenes(manifest, scenes)]


def voice_tasks(settings: Settings, manifest: ProjectManifest, scenes: list[str] | None = None) -> list[Task]:
    return [_t(settings, manifest.project_id, "voice", "scene", f"narration for {sid}", {"scene_id": sid}) for sid in _scenes(manifest, scenes)]


def video_tasks(settings: Settings, manifest: ProjectManifest, scenes: list[str] | None = None, force: bool = False) -> list[Task]:
    tasks: list[Task] = []
    for sid in _scenes(manifest, scenes):
        image = manifest.selected.get("image", {}).get(sid)
        if not image:
            raise ValidationFailed(f"scene {sid} has no image yet", hint="generate images first")
        voice = _t(settings, manifest.project_id, "voice", "scene", f"narration for {sid}", {"scene_id": sid})
        video = _t(settings, manifest.project_id, "videographer", "scene", f"clip for {sid}",
                   {"scene_id": sid, "force": force, "image": {"path": image, "scene_id": sid}, "voice": f"${{{voice.id}}}"}, [voice.id])  # fmt: skip
        tasks += [voice, video]
    return tasks


def render_tasks(settings: Settings, manifest: ProjectManifest) -> list[Task]:
    ids = _scenes(manifest)
    clips = manifest.selected.get("video", {})
    missing = [sid for sid in ids if sid not in clips]
    if missing:
        raise ValidationFailed(f"scenes without a clip: {', '.join(missing)}", hint="generate videos first")
    videos = [{"scene_id": sid, "path": clips[sid]} for sid in ids]
    voices = [_t(settings, manifest.project_id, "voice", "scene", f"narration for {sid}", {"scene_id": sid}) for sid in ids]
    refs = [f"${{{v.id}}}" for v in voices]
    deps = [v.id for v in voices]
    tasks = [*voices]
    inputs: dict[str, Any] = {"videos": videos, "voices": refs}
    if manifest.brief.subtitles:
        sub = _t(settings, manifest.project_id, "subtitles", "generate", "subtitles", {"videos": videos, "voices": refs}, deps)
        tasks.append(sub)
        inputs["subtitles"] = f"${{{sub.id}}}"
        deps = [*deps, sub.id]
    tasks.append(_t(settings, manifest.project_id, "editor", "render", "render the final video", inputs, deps, timeout=max(1800.0, settings.task_timeout)))
    return tasks


def qc_tasks(settings: Settings, project_id: str) -> list[Task]:
    return [_t(settings, project_id, "quality", "final", "final quality gates", {})]
