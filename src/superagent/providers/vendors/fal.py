"""fal.ai queue API (https://fal.ai/docs → Model APIs → Queue).

Submit to ``queue.fal.run/<model>``, poll the returned ``status_url`` until COMPLETED,
then fetch ``response_url``. As with Replicate, model-specific inputs are mapped through
``FAL_IMAGE_INPUT`` / ``FAL_VIDEO_INPUT`` JSON templates.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from ...errors import ProviderError
from ...media.images import fit_image, to_data_uri
from ...types import ProviderStatus, ProviderTier
from ...utils import stable_hash
from ..base import ImageProvider, ImageRequest, ImageResult, Readiness, VideoProvider, VideoRequest, VideoResult
from ..http import download, json_body, new_client, send
from ..templating import drop_none, fill_template, nearest_aspect
from .replicate import video_variables

QUEUE = "https://queue.fal.run"


class _Client:
    def __init__(self, key: str | None, queue_base: str = QUEUE):
        self.key = key
        self.queue_base = queue_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = new_client(headers={"Authorization": f"Key {self.key}"}, timeout=httpx.Timeout(60.0, connect=10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self, name: str, model: str, payload: dict[str, Any], *, timeout: float = 900.0) -> dict[str, Any]:
        submitted = json_body(name, await send(name, self.http, "POST", f"{self.queue_base}/{model}", json=payload))
        status_url, response_url = submitted.get("status_url"), submitted.get("response_url")
        if not status_url or not response_url:
            raise ProviderError(name, ProviderStatus.TEMPORARY_ERROR, "queue did not return tracking URLs")
        deadline = time.monotonic() + timeout
        while True:
            status = json_body(name, await send(name, self.http, "GET", status_url))
            state = str(status.get("status", "")).upper()
            if state == "COMPLETED":
                break
            if state not in {"IN_QUEUE", "IN_PROGRESS"}:
                detail = str(status.get("error") or state)[:300]
                raise ProviderError(name, ProviderStatus.UNAVAILABLE, f"request ended as {detail}", penalize=False)
            if time.monotonic() > deadline:
                raise ProviderError(name, ProviderStatus.TEMPORARY_ERROR, f"request did not finish within {timeout:g}s")
            await asyncio.sleep(1.5)
        result = json_body(name, await send(name, self.http, "GET", response_url))
        return result if isinstance(result, dict) else {"output": result}


def _file_urls(result: Any, prefer: tuple[str, ...]) -> list[str]:
    """URLs of generated files, looking first under the keys a model of this kind normally uses."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if isinstance(url, str) and url.startswith("http"):
                found.append(url)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    if isinstance(result, dict):
        for key in prefer:
            if key in result:
                walk(result[key])
        if not found:
            walk(result)
    return found


class FalImage(ImageProvider):
    name = "fal-image"
    tier = ProviderTier.CLOUD_PRIMARY
    description = "image models hosted on fal.ai"

    def __init__(self, key: str | None, model: str, extra_input: dict[str, Any] | None = None, queue_base: str = QUEUE):
        self._api = _Client(key, queue_base)
        self.model = model
        self.extra_input = extra_input or {}

    def readiness(self) -> Readiness:
        return Readiness.ready() if self._api.key else Readiness.not_configured("no API key (set FAL_KEY)")

    async def aclose(self) -> None:
        await self._api.aclose()

    async def generate(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        variables = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt, "width": request.width,
            "height": request.height, "seed": request.seed, "num_images": request.num_images,
            "aspect_ratio": nearest_aspect(request.width, request.height),
        }  # fmt: skip
        payload = drop_none({
            "prompt": request.prompt, "image_size": {"width": request.width, "height": request.height},
            "num_images": request.num_images, "seed": request.seed,
            **fill_template(self.extra_input, variables), **request.extra,
        })  # fmt: skip
        result = await self._api.run(self.name, str(self.model), payload)
        urls = _file_urls(result, ("images", "image"))
        if not urls:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "request completed but returned no images")
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = stable_hash({"u": urls, "t": time.time()}, 10)
        results: list[ImageResult] = []
        for index, url in enumerate(urls):
            dest = out_dir / f"{self.name}-{tag}-{index}.png"
            await download(self.name, self._api.http, url, dest, max_bytes=60_000_000)
            fit_image(dest, request.width, request.height)
            results.append(ImageResult(path=dest, provider=self.name, model=self.model,
                                       seed=result.get("seed", request.seed) if isinstance(result.get("seed"), int) else request.seed,
                                       width=request.width, height=request.height, metadata={"source_url": url}))  # fmt: skip
        return results


class FalVideo(VideoProvider):
    name = "fal-video"
    tier = ProviderTier.CLOUD_PRIMARY
    capabilities = frozenset({"text_to_video", "image_to_video"})
    description = "video models hosted on fal.ai"

    def __init__(
        self, key: str | None, model: str | None, image_key: str = "image_url",
        extra_input: dict[str, Any] | None = None, queue_base: str = QUEUE,
    ):  # fmt: skip
        self._api = _Client(key, queue_base)
        self.model = model
        self.image_key = image_key
        self.extra_input = extra_input or {}

    def readiness(self) -> Readiness:
        if not self._api.key:
            return Readiness.not_configured("no API key (set FAL_KEY)")
        if not self.model:
            return Readiness.not_configured("no video model selected (set FAL_VIDEO_MODEL)")
        return Readiness.ready()

    async def aclose(self) -> None:
        await self._api.aclose()

    async def text_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        return await self._run(request, out_path, image=None)

    async def image_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        if request.image is None:
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "image_to_video needs an image", penalize=False)
        return await self._run(request, out_path, image=request.image)

    async def _run(self, request: VideoRequest, out_path: Path, image: Path | None) -> VideoResult:
        payload: dict[str, Any] = {"prompt": request.prompt}
        if image is not None:
            payload[self.image_key] = to_data_uri(image)
        payload = drop_none({**payload, **fill_template(self.extra_input, video_variables(request)), **request.extra})
        result = await self._api.run(self.name, str(self.model), payload, timeout=1800.0)
        urls = _file_urls(result, ("video", "videos"))
        if not urls:
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "request completed but returned no video")
        await download(self.name, self._api.http, urls[0], out_path)
        return VideoResult(path=out_path, provider=self.name, model=self.model, metadata={"source_url": urls[0]})
