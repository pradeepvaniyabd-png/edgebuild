"""Generation engine: cached, resumable, best-of-N image and video generation with QC.

Every attempt is a ``generations`` row, so a restart resumes from the last good result
and an identical request is answered from cache instead of being generated again.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import Any

from ..db.repo import Repo
from ..errors import MediaError
from ..observability import get_logger
from ..providers.base import ImageRequest, MotionPlan, VideoRequest
from ..providers.services import AI
from ..types import GenerationStatus
from ..utils import file_sha256, stable_hash, utcnow
from .models import QUALITY, ProjectManifest, Scene
from .project import ProjectStore
from .qc import QCResult, qc_image_full, qc_video

log = get_logger("generation")


def _seed(manifest: ProjectManifest, scene: Scene) -> int:
    first = next((c for c in (manifest.story.characters if manifest.story else []) if c.character_id in scene.characters), None)
    base = first.seed if first and first.seed else int(stable_hash(manifest.project_id, 8), 16)
    return (base + sum(ord(ch) for ch in scene.scene_id) * 131) % 2_000_000_000


class GenerationEngine:
    def __init__(self, repo: Repo, store: ProjectStore):
        self.repo = repo
        self.store = store

    # ------------------------------------------------------------- helpers
    async def _cached(self, cache_key: str, force: bool) -> dict[str, Any] | None:
        if force:
            return None
        row = await self.repo.find_cached_generation(cache_key)
        if row and row["output_path"] and Path(row["output_path"]).is_file():
            return row
        return None

    async def _select(self, manifest: ProjectManifest, kind: str, scene_id: str, path: Path) -> None:
        def mutate(m: ProjectManifest) -> None:
            m.selected.setdefault(kind, {})[scene_id] = str(path)

        await self.store.update(manifest.project_id, mutate)

    # --------------------------------------------------------------- images
    async def scene_image(
        self, ai: AI, manifest: ProjectManifest, scene: Scene, *, execution_id: str | None = None, force: bool = False,
        takes: int | None = None, seed_bump: int = 0, previous: Path | None = None, only: str | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        brief = manifest.brief
        width, height = brief.size
        takes = takes or int(QUALITY[brief.quality]["image_takes"])
        seed = (_seed(manifest, scene) + seed_bump * 7919) % 2_000_000_000
        references = [Path(p) for c in (manifest.story.characters if manifest.story else []) if c.character_id in scene.characters
                      for p in c.reference_assets if Path(p).is_file()][:2]  # fmt: skip
        cache_key = stable_hash({"kind": "image", "prompt": scene.image_prompt, "negative": scene.negative_prompt, "w": width, "h": height,
                                 "seed": seed, "takes": takes, "refs": [file_sha256(p) for p in references], "only": only}, 40)  # fmt: skip
        target = self.store.path(manifest.project_id, "images", f"{scene.scene_id}.png")
        hit = await self._cached(cache_key, force)
        if hit:
            if Path(hit["output_path"]) != target:
                shutil.copy2(hit["output_path"], target)
            await self._select(manifest, "image", scene.scene_id, target)
            return {"path": str(target), "provider": hit["provider"], "model": hit["model"], "score": hit["score"], "cached": True,
                    "candidates": [], "qc": hit["qc"], "attempts": 0}  # fmt: skip

        candidates_dir = self.store.path(manifest.project_id, "images", "candidates", f"{scene.scene_id}.keep").parent
        rows = []
        for index in range(takes):
            rows.append(await self.repo.create_generation(
                project_id=manifest.project_id, execution_id=execution_id, scene_id=scene.scene_id, kind="image",
                status=GenerationStatus.GENERATING.value, prompt=scene.image_prompt,
                params={"seed": seed + index * 104_729, "width": width, "height": height, "take": index + 1},
            ))  # fmt: skip

        async def one(index: int) -> tuple[dict[str, Any], Path | None, QCResult | None, str | None, str | None]:
            row = rows[index]
            request = ImageRequest(prompt=scene.image_prompt, negative_prompt=scene.negative_prompt, width=width, height=height,
                                   seed=seed + index * 104_729, num_images=1, reference_images=references)  # fmt: skip
            try:
                results = await ai.image(request, candidates_dir, only=only)
            except Exception as exc:
                await self.repo.update_generation(row["id"], status=GenerationStatus.FAILED.value, error=str(exc)[:500], attempts=1)
                return row, None, None, None, str(exc)
            produced = results[0]
            await self.repo.update_generation(row["id"], status=GenerationStatus.QC.value, provider=produced.provider, model=produced.model,
                                              output_path=str(produced.path), attempts=1)  # fmt: skip
            qc = await qc_image_full(ai, produced.path, scene.image_prompt, width=width, height=height, reference=previous)
            return row, produced.path, qc, produced.provider, produced.model

        outcomes = await asyncio.gather(*(one(i) for i in range(takes)))
        valid = [(row, path, qc, provider, model) for row, path, qc, provider, model in outcomes if path and qc and qc.ok]
        for row, path, qc, *_ in outcomes:
            if path and qc:
                await self.repo.update_generation(row["id"], status=GenerationStatus.COMPLETED.value if qc.ok else GenerationStatus.FAILED.value,
                                                  score=qc.score, qc=qc.to_dict(), completed_at=utcnow(),
                                                  error=None if qc.ok else "; ".join(qc.problems)[:500])  # fmt: skip
        if not valid:
            reasons = sorted({p for _, _, qc, _, _ in outcomes if qc for p in qc.problems} | {err for *_, err in outcomes if isinstance(err, str) and err})
            raise MediaError(f"no usable image for scene {scene.scene_id}: {'; '.join(reasons)[:400] or 'generation failed'}")
        best_row, best_path, best_qc, provider, model = max(valid, key=lambda item: item[2].score)
        shutil.copy2(best_path, target)
        await self.repo.update_generation(best_row["id"], selected=True, cache_key=cache_key, output_path=str(target))
        await self._select(manifest, "image", scene.scene_id, target)
        await self.store.register_asset(manifest.project_id, "image", target, scene.scene_id, provider=provider, model=model,
                                        score=best_qc.score, takes=takes)  # fmt: skip
        return {"path": str(target), "provider": provider, "model": model, "score": round(best_qc.score, 4), "cached": False,
                "qc": best_qc.to_dict(), "attempts": takes,
                "candidates": [{"path": str(p), "score": round(q.score, 4) if q else None, "ok": bool(q and q.ok)} for _, p, q, *_ in outcomes if p]}  # fmt: skip

    # --------------------------------------------------------------- videos
    async def scene_video(
        self, ai: AI, manifest: ProjectManifest, scene: Scene, image: Path | None, *, duration: float | None = None,
        execution_id: str | None = None, force: bool = False, takes: int | None = None, seed_bump: int = 0, only: str | None = None,
        require_audio: bool = False,
    ) -> dict[str, Any]:  # fmt: skip
        brief = manifest.brief
        width, height = brief.size
        takes = takes or int(QUALITY[brief.quality]["video_takes"])
        duration = round(duration or scene.duration, 2)
        seed = (_seed(manifest, scene) + seed_bump * 7919) % 2_000_000_000
        operation = "image_to_video" if image is not None and image.is_file() else "text_to_video"
        cache_key = stable_hash({"kind": "video", "prompt": scene.video_prompt, "image": file_sha256(image) if image and image.is_file() else None,
                                 "dur": duration, "cam": scene.camera, "w": width, "h": height, "fps": brief.fps, "seed": seed, "takes": takes,
                                 "only": only}, 40)  # fmt: skip
        target = self.store.path(manifest.project_id, "videos", f"{scene.scene_id}.mp4")
        hit = await self._cached(cache_key, force)
        if hit:
            if Path(hit["output_path"]) != target:
                shutil.copy2(hit["output_path"], target)
            await self._select(manifest, "video", scene.scene_id, target)
            return {"path": str(target), "provider": hit["provider"], "model": hit["model"], "score": hit["score"], "cached": True,
                    "generative": bool((hit["params"] or {}).get("generative", True)), "qc": hit["qc"], "notes": [], "duration": duration}  # fmt: skip

        candidates_dir = self.store.path(manifest.project_id, "videos", "candidates", f"{scene.scene_id}.keep").parent
        rows = [
            await self.repo.create_generation(project_id=manifest.project_id, execution_id=execution_id, scene_id=scene.scene_id, kind="video",
                                              status=GenerationStatus.GENERATING.value, prompt=scene.video_prompt,
                                              params={"seed": seed + i * 104_729, "duration": duration, "operation": operation, "take": i + 1})  # fmt: skip
            for i in range(takes)
        ]

        async def one(index: int):  # noqa: ANN202
            row = rows[index]
            out = candidates_dir / f"{scene.scene_id}-t{index + 1}.mp4"
            request = VideoRequest(prompt=scene.video_prompt, negative_prompt=scene.negative_prompt, image=image, duration=duration,
                                   fps=brief.fps, width=width, height=height, seed=seed + index * 104_729,
                                   motion=MotionPlan(camera=scene.camera, intensity=0.55, subject=scene.action, environment=scene.environment))  # fmt: skip
            try:
                result = await ai.video(request, out, operation=operation, only=only)
            except Exception as exc:
                await self.repo.update_generation(row["id"], status=GenerationStatus.FAILED.value, error=str(exc)[:500], attempts=1)
                return row, None, None, None, str(exc)
            await self.repo.update_generation(row["id"], status=GenerationStatus.QC.value, provider=result.provider, model=result.model,
                                              output_path=str(result.path), attempts=1)  # fmt: skip
            qc = await qc_video(result.path, width=width, height=height, fps=brief.fps, expected_duration=duration,
                                require_audio=require_audio, expect_motion=result.generative)  # fmt: skip
            return row, result, qc, result.provider, None

        outcomes = await asyncio.gather(*(one(i) for i in range(takes)))
        for row, result, qc, *_ in outcomes:
            if result and qc:
                await self.repo.update_generation(row["id"], status=GenerationStatus.COMPLETED.value if qc.ok else GenerationStatus.FAILED.value,
                                                  score=qc.score, qc=qc.to_dict(), completed_at=utcnow(),
                                                  error=None if qc.ok else "; ".join(qc.problems)[:500])  # fmt: skip
        valid = [item for item in outcomes if item[1] and item[2] and item[2].ok]
        if not valid:
            reasons = sorted({p for _, _, qc, _, _ in outcomes if qc for p in qc.problems} | {err for *_, err in outcomes if isinstance(err, str) and err})
            raise MediaError(f"no usable video for scene {scene.scene_id}: {'; '.join(reasons)[:400] or 'generation failed'}")
        best_row, best, best_qc, provider, _ = max(valid, key=lambda item: item[2].score)
        shutil.copy2(best.path, target)
        await self.repo.update_generation(best_row["id"], selected=True, cache_key=cache_key, output_path=str(target),
                                          params={**(best_row["params"] or {}), "generative": best.generative})  # fmt: skip
        await self._select(manifest, "video", scene.scene_id, target)
        await self.store.register_asset(manifest.project_id, "video", target, scene.scene_id, provider=provider, model=best.model,
                                        score=best_qc.score, generative=best.generative)  # fmt: skip
        return {"path": str(target), "provider": provider, "model": best.model, "score": round(best_qc.score, 4), "cached": False,
                "generative": best.generative, "qc": best_qc.to_dict(), "notes": best.metadata.get("notes", []), "duration": duration}  # fmt: skip
