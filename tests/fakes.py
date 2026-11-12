"""Test doubles. Nothing in here is imported by application code.

``FakeVendors`` is a real HTTP server (FastAPI on a loopback port) that speaks just
enough of each vendor protocol to exercise the adapters over the wire: OpenAI-compatible,
Ollama, Anthropic Messages, Replicate, fal queue and ComfyUI.
"""

from __future__ import annotations

import base64
import io
import math
import socket
import struct
import threading
import time
import wave
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from PIL import Image

from superagent.providers.base import (
    ImageProvider,
    ImageRequest,
    ImageResult,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Readiness,
    Usage,
)
from superagent.types import ProviderTier


def png_bytes(width: int = 64, height: int = 64, color: tuple[int, int, int] = (200, 80, 40)) -> bytes:
    image = Image.new("RGB", (width, height), color)
    for x in range(0, width, 8):  # some structure so sharpness/contrast checks see a real picture
        for y in range(0, height, 8):
            if (x // 8 + y // 8) % 2 == 0:
                image.paste((255 - color[0], 255 - color[1], 255 - color[2]), (x, y, x + 8, y + 8))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def wav_bytes(seconds: float = 0.4, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 330 * i / rate))) for i in range(int(seconds * rate))
        )
        handle.writeframes(frames)
    return buffer.getvalue()


class FakeVendors:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.scripts: dict[str, list[tuple[int, Any, dict[str, str]]]] = defaultdict(list)
        self.chat: Callable[[dict[str, Any]], str] = lambda body: "pong"
        self.files: dict[str, bytes] = {"image.png": png_bytes(), "clip.mp4": b""}
        self.polls: dict[str, int] = defaultdict(int)
        self.base_url = ""
        self.app = self._build()

    # --------------------------------------------------------------- control
    def reset(self) -> None:
        self.calls.clear()
        self.scripts.clear()
        self.polls.clear()
        self.chat = lambda body: "pong"

    def script(self, route: str, status: int, body: Any = None, headers: dict[str, str] | None = None) -> None:
        """Queue a one-off response for ``route`` (e.g. 'chat', 'anthropic', 'images')."""
        self.scripts[route].append((status, body if body is not None else {"error": {"message": f"scripted {status}"}}, headers or {}))

    def requests(self, route: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["route"] == route]

    async def _log(self, route: str, request: Request) -> tuple[dict[str, Any], Response | None]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        self.calls.append({"route": route, "body": body, "headers": dict(request.headers), "query": dict(request.query_params)})
        if self.scripts[route]:
            status, payload, headers = self.scripts[route].pop(0)
            return body, JSONResponse(payload, status_code=status, headers=headers)
        return body, None

    # ------------------------------------------------------------------- app
    def _build(self) -> FastAPI:  # noqa: C901 - one route table
        app = FastAPI()
        fake = self

        # ---- OpenAI-compatible ------------------------------------------------
        @app.get("/v1/models")
        async def models(request: Request) -> Response:
            _, scripted = await fake._log("models", request)
            return scripted or JSONResponse({"data": [{"id": "fake-model"}]})

        @app.post("/v1/chat/completions")
        async def chat(request: Request) -> Response:
            body, scripted = await fake._log("chat", request)
            if scripted:
                return scripted
            text = fake.chat(body)
            return JSONResponse({
                "model": body.get("model"),
                "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            })  # fmt: skip

        @app.post("/v1/embeddings")
        async def embeddings(request: Request) -> Response:
            body, scripted = await fake._log("embeddings", request)
            if scripted:
                return scripted
            data = [{"index": i, "embedding": [float(len(t)), 1.0, 0.5]} for i, t in enumerate(body.get("input", []))]
            return JSONResponse({"data": list(reversed(data))})  # out of order on purpose

        @app.post("/v1/images/generations")
        async def images(request: Request) -> Response:
            body, scripted = await fake._log("images", request)
            if scripted:
                return scripted
            width, height = (int(v) for v in str(body.get("size", "64x64")).split("x"))
            item = {"b64_json": base64.b64encode(png_bytes(width // 8, height // 8)).decode()}
            return JSONResponse({"created": 1, "data": [item for _ in range(int(body.get("n", 1)))]})

        @app.post("/v1/audio/speech")
        async def speech(request: Request) -> Response:
            _, scripted = await fake._log("speech", request)
            return scripted or Response(wav_bytes(), media_type="audio/wav")

        # ---- Ollama -----------------------------------------------------------
        @app.get("/api/tags")
        async def tags(request: Request) -> Response:
            _, scripted = await fake._log("ollama-tags", request)
            return scripted or JSONResponse({"models": [{"name": "nomic-embed-text"}, {"name": "llama-test:latest"}]})

        @app.post("/api/chat")
        async def ollama_chat(request: Request) -> Response:
            body, scripted = await fake._log("ollama-chat", request)
            if scripted:
                return scripted
            return JSONResponse({
                "model": body.get("model"), "message": {"role": "assistant", "content": fake.chat(body)},
                "prompt_eval_count": 5, "eval_count": 3, "done_reason": "stop",
            })  # fmt: skip

        @app.post("/api/embed")
        async def ollama_embed(request: Request) -> Response:
            body, scripted = await fake._log("ollama-embed", request)
            return scripted or JSONResponse({"embeddings": [[0.1, 0.2] for _ in body.get("input", [])]})

        # ---- Anthropic Messages ----------------------------------------------
        @app.post("/v1/messages")
        async def anthropic(request: Request) -> Response:
            body, scripted = await fake._log("anthropic", request)
            if scripted:
                return scripted
            return JSONResponse({
                "id": "msg_fake", "type": "message", "role": "assistant", "model": body.get("model"),
                "content": [{"type": "text", "text": fake.chat(body)}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 12, "output_tokens": 4},
            })  # fmt: skip

        # ---- Replicate --------------------------------------------------------
        @app.post("/v1/models/{owner}/{name}/predictions")
        async def replicate_create(owner: str, name: str, request: Request) -> Response:
            _, scripted = await fake._log("replicate", request)
            if scripted:
                return scripted
            kind = "video" if "video" in name else "image"
            return JSONResponse({"id": f"p-{kind}", "status": "starting", "urls": {"get": f"{fake.base_url}/v1/predictions/p-{kind}"}})

        @app.get("/v1/predictions/{prediction_id}")
        async def replicate_get(prediction_id: str) -> Response:
            fake.polls[prediction_id] += 1
            if fake.polls[prediction_id] < 2:
                return JSONResponse({"id": prediction_id, "status": "processing", "urls": {"get": f"{fake.base_url}/v1/predictions/{prediction_id}"}})
            file = "clip.mp4" if "video" in prediction_id else "image.png"
            return JSONResponse({"id": prediction_id, "status": "succeeded", "output": [f"{fake.base_url}/files/{file}"]})

        # ---- fal queue --------------------------------------------------------
        @app.post("/fal/{model:path}")
        async def fal_submit(model: str, request: Request) -> Response:
            _, scripted = await fake._log("fal", request)
            if scripted:
                return scripted
            kind = "video" if "video" in model else "image"
            root = f"{fake.base_url}/falq/{kind}"
            return JSONResponse({"request_id": f"r-{kind}", "status_url": f"{root}/status", "response_url": f"{root}/response"})

        @app.get("/falq/{kind}/status")
        async def fal_status(kind: str) -> Response:
            fake.polls[f"fal-{kind}"] += 1
            done = fake.polls[f"fal-{kind}"] >= 2
            return JSONResponse({"status": "COMPLETED" if done else "IN_PROGRESS"})

        @app.get("/falq/{kind}/response")
        async def fal_response(kind: str) -> Response:
            if kind == "video":
                return JSONResponse({"video": {"url": f"{fake.base_url}/files/clip.mp4"}})
            return JSONResponse({"images": [{"url": f"{fake.base_url}/files/image.png"}], "seed": 77})

        # ---- ComfyUI ----------------------------------------------------------
        @app.get("/system_stats")
        async def comfy_stats() -> Response:
            return JSONResponse({"system": {"os": "fake"}})

        @app.post("/upload/image")
        async def comfy_upload(request: Request) -> Response:
            fake.calls.append({"route": "comfy-upload", "body": {}, "headers": dict(request.headers), "query": {}})
            return JSONResponse({"name": "uploaded.png", "subfolder": "", "type": "input"})

        @app.post("/prompt")
        async def comfy_prompt(request: Request) -> Response:
            _, scripted = await fake._log("comfy-prompt", request)
            return scripted or JSONResponse({"prompt_id": "c1", "number": 1, "node_errors": {}})

        @app.get("/history/{prompt_id}")
        async def comfy_history(prompt_id: str) -> Response:
            fake.polls["comfy"] += 1
            if fake.polls["comfy"] < 2:
                return JSONResponse({})
            image = {"filename": "ComfyUI_0001.png", "subfolder": "", "type": "output"}
            return JSONResponse({prompt_id: {"status": {"status_str": "success"}, "outputs": {"9": {"images": [image]}}}})

        @app.get("/view")
        async def comfy_view() -> Response:
            return Response(fake.files["image.png"], media_type="image/png")

        # ---- result files -----------------------------------------------------
        @app.get("/files/{name}")
        async def files(name: str) -> Response:
            return Response(fake.files[name], media_type="application/octet-stream")

        return app

    # ---------------------------------------------------------------- server
    def start(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(self.app, host="127.0.0.1", port=port, log_level="error", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.time() + 10
        while not self._server.started:
            if time.time() > deadline:
                raise RuntimeError("fake vendor server did not start")
            time.sleep(0.02)

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


# --------------------------------------------------------------- in-process doubles
class ScriptedLLM(LLMProvider):
    """An LLM double whose replies are scripted by the test."""

    tier = ProviderTier.LOCAL
    capabilities = frozenset({"chat", "vision"})

    def __init__(self, name: str = "scripted-llm", reply: Callable[[LLMRequest], str] | str = "ok"):
        self.name = name
        self.model = "scripted"
        self.reply = reply
        self.requests: list[LLMRequest] = []

    def readiness(self) -> Readiness:
        return Readiness.ready()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        text = self.reply(request) if callable(self.reply) else self.reply
        return LLMResponse(text=text, provider=self.name, model="scripted", usage=Usage(input_tokens=10, output_tokens=5))


class PaintedImages(ImageProvider):
    """An image double that writes real PNG files (flat colour derived from the seed)."""

    tier = ProviderTier.LOCAL

    def __init__(self, name: str = "painted-images"):
        self.name = name
        self.model = "painter"
        self.requests: list[ImageRequest] = []

    def readiness(self) -> Readiness:
        return Readiness.ready()

    async def generate(self, request: ImageRequest, out_dir: Path) -> list[ImageResult]:
        self.requests.append(request)
        out_dir.mkdir(parents=True, exist_ok=True)
        results = []
        for index in range(request.num_images):
            seed = (request.seed or 0) + index
            color = ((seed * 53) % 200 + 30, (seed * 97) % 200 + 30, (seed * 31) % 200 + 30)
            path = out_dir / f"{self.name}-{len(self.requests)}-{seed}-{index}.png"
            path.write_bytes(png_bytes(request.width, request.height, color))
            results.append(ImageResult(path=path, provider=self.name, model=self.model, seed=seed,
                                       width=request.width, height=request.height))  # fmt: skip
        return results


def pdf_bytes(lines: list[str]) -> bytes:
    """A minimal single-page PDF with a real text layer (Latin text only)."""
    content = "BT /F1 12 Tf 50 750 Td 14 TL " + " ".join(
        "(" + line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*" for line in lines
    ) + " ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content.encode("latin-1")),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % index + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(out)


def synthetic_candles(days: int = 6, step_minutes: int = 15, seed: int = 7) -> list[tuple[str, float, float, float, float, int]]:
    """Deterministic 15-minute OHLCV rows spanning several UTC days, drifting up then down."""
    import random
    from datetime import datetime, timedelta

    rng = random.Random(seed)
    price = 2300.0
    start = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
    rows = []
    for index in range(days * 24 * 60 // step_minutes):
        moment = start + timedelta(minutes=index * step_minutes)
        drift = 0.25 if index < days * 48 else -0.25
        open_ = price
        close = open_ + drift + rng.uniform(-2.5, 2.5)
        high = max(open_, close) + rng.uniform(0.2, 1.8)
        low = min(open_, close) - rng.uniform(0.2, 1.8)
        rows.append((moment.strftime("%Y-%m-%d %H:%M:%S"), round(open_, 2), round(high, 2), round(low, 2), round(close, 2), int(rng.uniform(80, 400))))
        price = close
    return rows
