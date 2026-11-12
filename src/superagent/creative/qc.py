"""Quality control for generated media.

What is measured is stated plainly. Technical checks (decodes, size, black/frozen frames,
blank or flat images, loudness) are exact. Whether a picture *matches its prompt* or has
visual artefacts can only be judged by a vision model; without one that part is reported
as "not assessed" rather than scored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..errors import SuperAgentError
from ..media.images import color_signature, histogram_similarity, technical_metrics
from ..media.probe import probe, probe_image
from ..observability import get_logger
from ..proc import run_process
from ..providers.services import AI
from ..types import ProviderKind

log = get_logger("qc")

_BLACK = re.compile(r"black_duration:([\d.]+)")
_FREEZE = re.compile(r"freeze_duration: ([\d.]+)")
_MEAN_VOLUME = re.compile(r"mean_volume: (-?[\d.]+|-inf) dB")
_MAX_VOLUME = re.compile(r"max_volume: (-?[\d.]+|-inf) dB")


@dataclass
class QCResult:
    ok: bool
    score: float
    metrics: dict[str, Any] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    assessed: list[str] = field(default_factory=list)
    not_assessed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "score": round(self.score, 4), "metrics": self.metrics, "problems": self.problems,
                "warnings": self.warnings, "assessed": self.assessed, "not_assessed": self.not_assessed}  # fmt: skip


class VisionRating(BaseModel):
    alignment: float = Field(ge=0, le=10)
    artifacts: float = Field(ge=0, le=10, description="10 means no visible artefacts")
    notes: str = ""


# --------------------------------------------------------------------- images
def qc_image(path: Path, *, width: int, height: int, aspect_tolerance: float = 0.03, reference: Path | None = None) -> QCResult:
    problems: list[str] = []
    warnings: list[str] = []
    try:
        info = probe_image(path)
    except SuperAgentError as exc:
        return QCResult(False, 0.0, problems=[exc.message], assessed=["decodes"])
    metrics: dict[str, Any] = {"width": info.width, "height": info.height, "format": info.format, "bytes": info.size_bytes}
    if (info.width, info.height) != (width, height):
        if abs(info.aspect_ratio - width / height) > aspect_tolerance:
            problems.append(f"aspect ratio {info.aspect_ratio:.3f} differs from the requested {width / height:.3f}")
        else:
            warnings.append(f"size {info.width}x{info.height} differs from {width}x{height}")
    tech = technical_metrics(path)
    metrics.update({k: round(v, 4) for k, v in tech.items()})
    if tech["dominant_share"] > 0.92 or tech["contrast"] < 0.03:
        problems.append("the image is blank or a flat colour")
    elif tech["sharpness"] < 0.006:
        warnings.append("the image is very soft or featureless")
    if tech["brightness"] < 0.03 or tech["brightness"] > 0.97:
        warnings.append("the image is almost entirely black or white")
    sharp = min(1.0, tech["sharpness"] / 0.05)
    contrast = min(1.0, tech["contrast"] / 0.35)
    spread = 1.0 - max(0.0, tech["dominant_share"] - 0.25) / 0.75
    score = 0.45 * sharp + 0.30 * contrast + 0.25 * spread
    assessed = ["decodes", "size/aspect", "blank/flat", "sharpness", "contrast"]
    if reference is not None and reference.is_file():
        similarity = histogram_similarity(color_signature(path), color_signature(reference))
        metrics["palette_consistency"] = round(similarity, 3)
        assessed.append("palette consistency with the previous selected frame")
        score = 0.85 * score + 0.15 * similarity
        if similarity < 0.2:
            warnings.append("the colour palette differs strongly from the previous scene")
    return QCResult(not problems, score if not problems else 0.0, metrics, problems, warnings, assessed,
                    ["prompt alignment", "anatomy/artefacts", "character likeness"])  # fmt: skip


async def vision_review(ai: AI, path: Path, prompt: str) -> VisionRating | None:
    """Ask a vision-capable model to rate the picture. Returns None when none is available."""
    if not ai.has(ProviderKind.LLM, "vision"):
        return None
    question = (
        f"You are a strict image QC reviewer. The image was generated from this prompt:\n{prompt[:1200]}\n\n"
        "Rate 0-10 how well it matches the prompt (alignment) and how free it is of visible defects such as extra or "
        "distorted limbs, garbled text, melted shapes or seams (artifacts; 10 = none)."
    )
    try:
        return await ai.complete_json(question, VisionRating, max_tokens=400, attempts=1)  # type: ignore[arg-type]
    except Exception:
        log.info("vision review unavailable for %s", path.name, exc_info=True)
        return None


async def qc_image_full(ai: AI | None, path: Path, prompt: str, *, width: int, height: int, reference: Path | None = None,
                        review: bool = True) -> QCResult:  # fmt: skip
    result = qc_image(path, width=width, height=height, reference=reference)
    if not result.ok or ai is None or not review:
        return result
    rating = await vision_review(ai.scoped(), path, prompt)
    if rating is None:
        return result
    result.metrics.update(alignment=rating.alignment, artifacts=rating.artifacts, review_notes=rating.notes[:200])
    result.assessed += ["prompt alignment (vision model)", "artefacts (vision model)"]
    result.not_assessed = [item for item in result.not_assessed if item not in {"prompt alignment", "anatomy/artefacts"}]
    result.score = 0.4 * result.score + 0.6 * (0.6 * rating.alignment + 0.4 * rating.artifacts) / 10
    if rating.alignment < 3 or rating.artifacts < 3:
        result.ok = False
        result.problems.append(f"vision review rejected the image (alignment {rating.alignment:g}, artifacts {rating.artifacts:g})")
        result.score = 0.0
    return result


# --------------------------------------------------------------------- video
async def _filter_scan(path: Path, video_filter: str) -> str:
    result = await run_process(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path), "-an", "-vf", video_filter,
                                "-f", "null", "-"], timeout=300, raise_on_timeout=False)  # fmt: skip
    return result.stderr


async def black_and_freeze(path: Path) -> tuple[float, float]:
    black = sum(float(x) for x in _BLACK.findall(await _filter_scan(path, "blackdetect=d=0.15:pix_th=0.10")))
    freeze = sum(float(x) for x in _FREEZE.findall(await _filter_scan(path, "freezedetect=n=-55dB:d=0.6")))
    return black, freeze


async def qc_video(path: Path, *, width: int, height: int, fps: float, expected_duration: float, duration_tolerance: float = 1.0,
                   require_audio: bool = False, expect_motion: bool = True) -> QCResult:  # fmt: skip
    problems: list[str] = []
    warnings: list[str] = []
    try:
        info = await probe(path)
    except SuperAgentError as exc:
        return QCResult(False, 0.0, problems=[exc.message], assessed=["decodes"])
    metrics: dict[str, Any] = {"container": info.container, "duration": round(info.duration, 3), "bytes": info.size_bytes}
    if info.video is None:
        return QCResult(False, 0.0, metrics, ["no video stream"], assessed=["decodes"])
    video = info.video
    metrics.update(codec=video.codec, width=video.width, height=video.height, fps=round(video.fps, 2), pix_fmt=video.pix_fmt,
                   has_audio=info.has_audio)  # fmt: skip
    if (video.width, video.height) != (width, height):
        problems.append(f"resolution {video.width}x{video.height} is not the requested {width}x{height}")
    if abs(video.fps - fps) > 1.0:
        warnings.append(f"frame rate {video.fps:.1f} differs from {fps:g}")
    if abs(info.duration - expected_duration) > duration_tolerance:
        short = info.duration < expected_duration
        (problems if short and info.duration < expected_duration * 0.6 else warnings).append(
            f"duration {info.duration:.2f}s differs from the expected {expected_duration:.2f}s"
        )
    if require_audio and not info.has_audio:
        problems.append("no audio stream")
    black, freeze = await black_and_freeze(path)
    black_ratio = min(1.0, black / info.duration) if info.duration else 1.0
    freeze_ratio = min(1.0, freeze / info.duration) if info.duration else 1.0
    metrics.update(black_ratio=round(black_ratio, 3), freeze_ratio=round(freeze_ratio, 3))
    if black_ratio > 0.5:
        problems.append(f"{black_ratio:.0%} of the clip is black")
    elif black_ratio > 0.1:
        warnings.append(f"{black_ratio:.0%} of the clip is black")
    if expect_motion and freeze_ratio > 0.6:
        warnings.append(f"{freeze_ratio:.0%} of the clip is frozen")
    score = max(0.0, 1.0 - 0.7 * black_ratio - 0.3 * (freeze_ratio if expect_motion else 0.0))
    return QCResult(not problems, score if not problems else 0.0, metrics, problems, warnings,
                    ["decodes", "resolution", "frame rate", "duration", "black frames", "frozen frames"],
                    ["visual artefacts", "temporal flicker", "character continuity"])  # fmt: skip


async def audio_levels(path: Path) -> dict[str, float | None]:
    result = await run_process(["ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", str(path), "-vn", "-af", "volumedetect",
                                "-f", "null", "-"], timeout=300, raise_on_timeout=False)  # fmt: skip

    def parse(pattern: re.Pattern[str]) -> float | None:
        found = pattern.search(result.stderr)
        if not found or found.group(1) == "-inf":
            return None
        return float(found.group(1))

    return {"mean_db": parse(_MEAN_VOLUME), "max_db": parse(_MAX_VOLUME)}
