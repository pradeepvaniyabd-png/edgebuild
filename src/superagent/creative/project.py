"""Project folders and the manifest. Everything a project produces lives under one directory."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..config import Settings
from ..db.repo import Repo
from ..errors import FileProblem
from ..utils import file_sha256, new_id, slugify
from .models import CreativeBrief, ProjectManifest

SUBDIRS = ("script", "storyboard", "characters", "locations", "references", "images", "videos", "audio", "subtitles",
           "thumbnails", "renders", "metadata", "logs", "exports")  # fmt: skip


class ProjectStore:
    def __init__(self, settings: Settings, repo: Repo):
        self.settings = settings
        self.repo = repo

    def root(self, project_id: str) -> Path:
        return self.settings.projects_dir / project_id

    def path(self, project_id: str, *parts: str) -> Path:
        target = self.root(project_id).joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    async def create(self, brief: CreativeBrief, name: str | None = None) -> ProjectManifest:
        label = name or brief.title or brief.idea
        project_id = f"{slugify(label, 28)}-{new_id('p')[-8:]}"
        root = self.root(project_id)
        for sub in SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
        manifest = ProjectManifest(project_id=project_id, name=label[:120], brief=brief)
        manifest.style.aspect_ratio = brief.aspect_ratio
        await self.repo.create_project(id=project_id, name=manifest.name, prompt=brief.idea, status="CREATED",
                                       brief=brief.model_dump(mode="json"), root_dir=str(root))  # fmt: skip
        self.save_manifest(manifest)
        return manifest

    # ------------------------------------------------------------- manifest
    def manifest_path(self, project_id: str) -> Path:
        return self.root(project_id) / "project.json"

    def load(self, project_id: str) -> ProjectManifest:
        path = self.manifest_path(project_id)
        if not path.is_file():
            raise FileProblem(f"unknown project '{project_id}'", hint="list projects with `super-agent projects`")
        return ProjectManifest.model_validate_json(path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: ProjectManifest) -> None:
        path = self.manifest_path(manifest.project_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
        os.replace(temp, path)  # atomic: a crash never leaves a half-written manifest

    async def update(self, project_id: str, mutate) -> ProjectManifest:  # noqa: ANN001
        """Load, apply ``mutate(manifest)``, save and mirror the status to the database."""
        manifest = self.load(project_id)
        mutate(manifest)
        self.save_manifest(manifest)
        await self.repo.update_project(project_id, status=manifest.status, brief=manifest.brief.model_dump(mode="json"))
        return manifest

    # --------------------------------------------------------------- assets
    async def register_asset(self, project_id: str, kind: str, path: Path, scene_id: str | None = None, **metadata: object) -> dict:
        return await self.repo.add_asset(project_id=project_id, scene_id=scene_id, kind=kind, path=str(path),
                                         meta=metadata, checksum=file_sha256(path) if path.is_file() else None)  # fmt: skip

    def import_file(self, project_id: str, source: Path, folder: str = "references") -> Path:
        target = self.path(project_id, folder, source.name)
        shutil.copy2(source, target)
        return target

    def listing(self, project_id: str) -> dict[str, list[str]]:
        root = self.root(project_id)
        return {sub: sorted(p.name for p in (root / sub).glob("*") if p.is_file()) for sub in SUBDIRS if (root / sub).is_dir()}

    def dump_json(self, project_id: str, folder: str, name: str, data: object) -> Path:
        path = self.path(project_id, folder, name)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return path
