"""ComfyUI: fully local image/video generation driven by the user's own workflow.

Export a workflow with "Save (API Format)" and put placeholders where the app should
inject values: ``{{prompt}}``, ``{{negative_prompt}}``, ``{{seed}}``, ``{{width}}``,
``{{height}}``, and for image-conditioned workflows ``{{image}}`` (the uploaded file name).
Video workflows may also use ``{{frames}}``, ``{{fps}}`` and ``{{duration}}``.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from ...errors import ProviderError
from ...media.images import fit_image
from ...types import ProviderStatus, ProviderTier
from ..base import ImageProvider, ImageRequest, ImageResult, Readiness, VideoProvider, VideoRequest, VideoResult
from ..http import json_body, new_client, send
from ..templating import fill_template, placeholders

_OUTPUT_KEYS = ("images", "gifs", "videos", "video", "animated")


class _Comfy:
    def __init__(self, name: str, base_url: str | None, workflow: Path | None, env_hint: str):
        self.name = name
        self.base_url = (base_url or "").rstrip("/")
        self.workflow = workflow
        self.env_hint = env_hint
        self._client: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = new_client(timeout=httpx.Timeout(120.0, connect=5.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def readiness(self) -> Readiness:
        if not self.base_url:
            return Readiness.not_configured("no ComfyUI server (set COMFYUI_BASE_URL)")
        if self.workflow is None:
            return Readiness.not_configured(f"no workflow file (set {self.env_hint})")
        if not self.workflow.is_file():
            return Readiness.not_configured(f"workflow file not found: {self.workflow}")
        try:
            self.load()
        except (ValueError, OSError) as exc:
            return Readiness.not_configured(f"workflow is not valid API-format JSON: {exc}")
        return Readiness.ready()

    def load(self) -> dict[str, Any]:
        assert self.workflow is not None
        data = json.loads(self.workflow.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not all(isinstance(node, dict) and "class_type" in node for node in data.values()):
            raise ValueError("expected the 'Save (API Format)' export: an object of nodes with class_type")
        return data

    def needs_image(self) -> bool:
        return "image" in placeholders(self.load())

    async def probe(self) -> None:
        await send(self.name, self.http, "GET", f"{self.base_url}/system_stats")

    async def upload(self, image: Path) -> str:
        files = {"image": (image.name, image.read_bytes(), "application/octet-stream")}
        response = await send(self.name, self.http, "POST", f"{self.base_url}/upload/image", files=files, data={"overwrite": "true"})
        name = json_body(self.name, response).get("name")
        if not name:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "image upload returned no file name")
        return str(name)

    async def run(self, variables: dict[str, Any], out_dir: Path, stem: str, *, timeout: float) -> list[Path]:
        graph = fill_template(self.load(), variables)
        response = await send(
            self.name, self.http, "POST", f"{self.base_url}/prompt", json={"prompt": graph, "client_id": uuid.uuid4().hex}
        )
        submitted = json_body(self.name, response)
        prompt_id = submitted.get("prompt_id")
        if not prompt_id:
            detail = json.dumps(submitted.get("node_errors") or submitted)[:300]
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, f"workflow was rejected: {detail}", penalize=False)

        deadline = time.monotonic() + timeout
        entry: dict[str, Any] | None = None
        while entry is None:
            if time.monotonic() > deadline:
                raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, f"workflow did not finish within {timeout:g}s")
            await asyncio.sleep(1.5)
            history = json_body(self.name, await send(self.name, self.http, "GET", f"{self.base_url}/history/{prompt_id}"))
            entry = history.get(prompt_id)
        status = entry.get("status") or {}
        if status.get("status_str") == "error":
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, f"workflow failed: {json.dumps(status)[:300]}", penalize=False)

        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for node_output in (entry.get("outputs") or {}).values():
            for key in _OUTPUT_KEYS:
                for item in node_output.get(key) or []:
                    if not isinstance(item, dict) or not item.get("filename"):
                        continue
                    params = {"filename": item["filename"], "subfolder": item.get("subfolder", ""), "type": item.get("type", "output")}
                    data = await send(self.name, self.http, "GET", f"{self.base_url}/view", params=params)
                    dest = out_dir / f"{stem}-{len(saved)}{Path(item['filename']).suffix or '.png'}"
                    dest.write_bytes(data.content)
                    saved.append(dest)
        if not saved:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "workflow finished but produced no files")
        return saved


class ComfyUIImage(ImageProvider):
    name = "comfyui-image"
    tier = ProviderTier.LOCAL
    description = "local diffusion through your ComfyUI workflow"

    def __init__(self, base_url: str | None, workflow: Path | None):
        self._comfy = _Comfy(self.name, base_url, workflow, "COMFYUI_IMAGE_WORKFLOW")
        self.model = workflow.name if workflow else None

    @property
    def capabilities(self) -> frozenset[str]:  # type: ignore[override]
        ready = self._comfy.readiness().ok
        return frozenset({"generate", "reference"} if ready and self._comfy.needs_image() else {"generate"})

    def readiness(self) -> Readiness:
        return self._comfy.readiness()

    async def probe(self) -> None:
        await self._comfy.probe()

    async def aclose(self) -> None:
        await self._comfy.aclose()

    async def generate(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        seed = request.seed if request.seed is not None else int(time.time() * 1000) % 2_000_000_000
        variables: dict[str, Any] = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt, "seed": seed,
            "width": request.width, "height": request.height, "batch_size": request.num_images,
        }  # fmt: skip
        if request.reference_images and self._comfy.needs_image():
            variables["image"] = await self._comfy.upload(request.reference_images[0])
        paths = await self._comfy.run(variables, out_dir, f"{self.name}-{seed}", timeout=1800.0)
        results = []
        for path in paths:
            fit_image(path, request.width, request.height)
            results.append(ImageResult(path=path, provider=self.name, model=self.model, seed=seed,
                                       width=request.width, height=request.height))  # fmt: skip
        return results


class ComfyUIVideo(VideoProvider):
    name = "comfyui-video"
    tier = ProviderTier.LOCAL
    capabilities = frozenset({"text_to_video", "image_to_video"})
    description = "local video generation through your ComfyUI workflow"

    def __init__(self, base_url: str | None, workflow: Path | None):
        self._comfy = _Comfy(self.name, base_url, workflow, "COMFYUI_VIDEO_WORKFLOW")
        self.model = workflow.name if workflow else None

    def readiness(self) -> Readiness:
        return self._comfy.readiness()

    async def probe(self) -> None:
        await self._comfy.probe()

    async def aclose(self) -> None:
        await self._comfy.aclose()

    async def text_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        return await self._run(request, out_path)

    async def image_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        if request.image is None:
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "image_to_video needs an image", penalize=False)
        if not self._comfy.needs_image():
            raise ProviderError(
                self.name, ProviderStatus.UNAVAILABLE, "the video workflow has no {{image}} placeholder", penalize=False
            )
        return await self._run(request, out_path)

    async def _run(self, request: VideoRequest, out_path: Path) -> VideoResult:
        seed = request.seed if request.seed is not None else int(time.time() * 1000) % 2_000_000_000
        variables: dict[str, Any] = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt, "seed": seed, "width": request.width,
            "height": request.height, "fps": request.fps, "duration": request.duration,
            "frames": max(1, int(round(request.duration * request.fps))), "camera": request.motion.camera,
        }  # fmt: skip
        if request.image is not None and self._comfy.needs_image():
            variables["image"] = await self._comfy.upload(request.image)
        paths = await self._comfy.run(variables, out_path.parent, out_path.stem + "-raw", timeout=3600.0)
        produced = next((p for p in paths if p.suffix.lower() in {".mp4", ".webm", ".mov", ".gif", ".webp"}), paths[0])
        if produced.suffix.lower() == out_path.suffix.lower():
            produced.replace(out_path)
        else:  # normalise whatever the workflow emitted into the requested container
            from ...media.ffmpeg import FASTSTART, VIDEO_ENCODE, ffmpeg

            await ffmpeg(["-i", produced, *VIDEO_ENCODE, *FASTSTART, "-an", out_path])
            produced.unlink(missing_ok=True)
        return VideoResult(path=out_path, provider=self.name, model=self.model, metadata={"seed": seed})
