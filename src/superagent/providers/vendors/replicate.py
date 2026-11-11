"""Replicate HTTP API (https://replicate.com/docs/reference/http).

Model inputs differ per model, so only the widely shared fields are sent by default
and ``REPLICATE_IMAGE_INPUT`` / ``REPLICATE_VIDEO_INPUT`` (JSON, with ``{{placeholders}}``)
let the user map the rest for whichever model they choose.
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

API = "https://api.replicate.com/v1"
_DONE = {"succeeded", "successful"}
_FAILED = {"failed", "canceled"}


class _Client:
    def __init__(self, token: str | None, api_base: str = API):
        self.token = token
        self.api_base = api_base.rstrip("/")
        self._client: httpx.AsyncClient | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = new_client(
                headers={"Authorization": f"Bearer {self.token}"}, timeout=httpx.Timeout(90.0, connect=10.0)
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(self, name: str, model: str, payload: dict[str, Any], *, timeout: float = 900.0) -> list[str]:
        """Create a prediction, wait for it and return the output file URLs."""
        if ":" in model:  # community model pinned to a version
            url, body = f"{self.api_base}/predictions", {"version": model.split(":", 1)[1], "input": payload}
        else:
            url, body = f"{self.api_base}/models/{model}/predictions", {"input": payload}
        response = await send(name, self.http, "POST", url, json=body, headers={"Prefer": "wait"})
        prediction = json_body(name, response)
        deadline = time.monotonic() + timeout
        while prediction.get("status") not in _DONE | _FAILED:
            if time.monotonic() > deadline:
                raise ProviderError(name, ProviderStatus.TEMPORARY_ERROR, f"prediction did not finish within {timeout:g}s")
            await asyncio.sleep(2.0)
            poll_url = (prediction.get("urls") or {}).get("get") or f"{self.api_base}/predictions/{prediction.get('id')}"
            prediction = json_body(name, await send(name, self.http, "GET", poll_url))
        if prediction.get("status") in _FAILED:
            detail = str(prediction.get("error") or prediction.get("status"))[:300]
            raise ProviderError(name, ProviderStatus.UNAVAILABLE, f"prediction {prediction.get('status')}: {detail}", penalize=False)
        urls = _urls(prediction.get("output"))
        if not urls:
            raise ProviderError(name, ProviderStatus.TEMPORARY_ERROR, "prediction succeeded but returned no files")
        return urls


def _urls(output: Any) -> list[str]:
    if isinstance(output, str):
        return [output] if output.startswith("http") else []
    if isinstance(output, list):
        return [url for item in output for url in _urls(item)]
    if isinstance(output, dict):
        return [url for item in output.values() for url in _urls(item)]
    return []


class ReplicateImage(ImageProvider):
    name = "replicate-image"
    tier = ProviderTier.CLOUD_PRIMARY
    description = "image models hosted on Replicate"

    def __init__(self, token: str | None, model: str, extra_input: dict[str, Any] | None = None, api_base: str = API):
        self._api = _Client(token, api_base)
        self.model = model
        self.extra_input = extra_input or {}

    def readiness(self) -> Readiness:
        if not self._api.token:
            return Readiness.not_configured("no API token (set REPLICATE_API_TOKEN)")
        return Readiness.ready()

    async def aclose(self) -> None:
        await self._api.aclose()

    async def generate(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        variables = {
            "prompt": request.prompt, "negative_prompt": request.negative_prompt, "width": request.width,
            "height": request.height, "seed": request.seed, "num_images": request.num_images,
            "aspect_ratio": nearest_aspect(request.width, request.height),
        }  # fmt: skip
        payload = drop_none({
            "prompt": request.prompt, "aspect_ratio": variables["aspect_ratio"], "num_outputs": request.num_images,
            "output_format": "png", "seed": request.seed,
            **fill_template(self.extra_input, variables), **request.extra,
        })  # fmt: skip
        urls = await self._api.run(self.name, str(self.model), payload)
        out_dir.mkdir(parents=True, exist_ok=True)
        tag = stable_hash({"u": urls, "t": time.time()}, 10)
        results: list[ImageResult] = []
        for index, url in enumerate(urls):
            dest = out_dir / f"{self.name}-{tag}-{index}.png"
            await download(self.name, self._api.http, url, dest, max_bytes=60_000_000)
            fit_image(dest, request.width, request.height)
            results.append(ImageResult(path=dest, provider=self.name, model=self.model, seed=request.seed,
                                       width=request.width, height=request.height, metadata={"source_url": url}))  # fmt: skip
        return results


class ReplicateVideo(VideoProvider):
    name = "replicate-video"
    tier = ProviderTier.CLOUD_PRIMARY
    capabilities = frozenset({"text_to_video", "image_to_video"})
    description = "video models hosted on Replicate"

    def __init__(
        self, token: str | None, model: str | None, image_key: str = "image",
        extra_input: dict[str, Any] | None = None, api_base: str = API,
    ):  # fmt: skip
        self._api = _Client(token, api_base)
        self.model = model
        self.image_key = image_key
        self.extra_input = extra_input or {}

    def readiness(self) -> Readiness:
        if not self._api.token:
            return Readiness.not_configured("no API token (set REPLICATE_API_TOKEN)")
        if not self.model:
            return Readiness.not_configured("no video model selected (set REPLICATE_VIDEO_MODEL)")
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
        variables = video_variables(request)
        payload: dict[str, Any] = {"prompt": request.prompt}
        if image is not None:
            payload[self.image_key] = to_data_uri(image)
        payload = drop_none({**payload, **fill_template(self.extra_input, variables), **request.extra})
        urls = await self._api.run(self.name, str(self.model), payload, timeout=1800.0)
        await download(self.name, self._api.http, urls[0], out_path)
        return VideoResult(path=out_path, provider=self.name, model=self.model, metadata={"source_url": urls[0]})


def video_variables(request: VideoRequest) -> dict[str, Any]:
    return {
        "prompt": request.prompt, "negative_prompt": request.negative_prompt, "duration": request.duration,
        "duration_int": int(round(request.duration)), "fps": request.fps, "width": request.width,
        "height": request.height, "seed": request.seed, "camera": request.motion.camera,
        "aspect_ratio": nearest_aspect(request.width, request.height, ["16:9", "9:16", "1:1", "4:3", "3:4"]),
    }  # fmt: skip
