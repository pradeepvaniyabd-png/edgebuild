"""Camera motion from a still image with FFmpeg (the "Ken Burns" technique).

This is NOT generative video: nothing in the picture moves, only the virtual camera.
It exists so a storyboard can always be turned into a watchable clip offline, and it
is routed last so any configured generative provider is preferred.
"""

from __future__ import annotations

from pathlib import Path

from ...errors import ProviderError
from ...media.ffmpeg import FASTSTART, VIDEO_ENCODE, ffmpeg
from ...proc import which
from ...types import ProviderStatus, ProviderTier
from ..base import Readiness, VideoProvider, VideoRequest, VideoResult

SUPPORTED_MOVES = ("static", "push-in", "pull-out", "pan-left", "pan-right", "tilt-up", "tilt-down", "handheld")
_ALIASES = {
    "zoom-in": "push-in", "dolly": "push-in", "dolly-in": "push-in", "slow-push": "push-in", "slow motion": "push-in",
    "zoom-out": "pull-out", "dolly-out": "pull-out", "reveal": "pull-out",
    "pan": "pan-right", "tracking": "pan-right", "orbit": "pan-right", "parallax": "pan-left",
    "tilt": "tilt-up", "crane": "tilt-up", "shake": "handheld",
}  # fmt: skip


def resolve_move(camera: str) -> tuple[str, bool]:
    """Map a requested camera move to a supported one. Returns (move, approximated)."""
    key = camera.strip().lower().replace("_", "-")
    if key in SUPPORTED_MOVES:
        return key, False
    return _ALIASES.get(key, "push-in"), True


def motion_filter(move: str, intensity: float, width: int, height: int, frames: int, fps: int) -> str:
    amount = round(0.06 + 0.24 * max(0.0, min(1.0, intensity)), 4)
    last = max(1, frames - 1)
    centre_x, centre_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    travel_x, travel_y = "(iw-iw/zoom)", "(ih-ih/zoom)"
    progress = f"on/{last}"
    zoom, x, y = f"{1 + amount}", centre_x, centre_y
    if move == "static":
        zoom = "1"
    elif move == "push-in":
        zoom = f"1+{amount}*{progress}"
    elif move == "pull-out":
        zoom = f"{1 + amount}-{amount}*{progress}"
    elif move == "pan-right":
        x = f"{travel_x}*{progress}"
    elif move == "pan-left":
        x = f"{travel_x}*(1-{progress})"
    elif move == "tilt-down":
        y = f"{travel_y}*{progress}"
    elif move == "tilt-up":
        y = f"{travel_y}*(1-{progress})"
    elif move == "handheld":
        zoom = "1.08"
        x = f"{travel_x}/2+{travel_x}/2*0.6*sin(on/11)"
        y = f"{travel_y}/2+{travel_y}/2*0.6*cos(on/17)"
    # Work on a 1.5x canvas so sub-pixel camera steps do not shimmer.
    canvas_w, canvas_h = int(width * 1.5) // 2 * 2, int(height * 1.5) // 2 * 2
    return (
        f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=increase,crop={canvas_w}:{canvas_h},"
        f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps},format=yuv420p"
    )


class FFmpegMotionVideo(VideoProvider):
    name = "ffmpeg-motion"
    tier = ProviderTier.LOCAL_FALLBACK
    capabilities = frozenset({"image_to_video"})
    model = "zoompan"
    description = "camera motion over a still image (not generative; nothing inside the frame moves)"

    def readiness(self) -> Readiness:
        if which("ffmpeg") is None:
            return Readiness.not_configured("ffmpeg is not installed")
        return Readiness.ready()

    async def image_to_video(self, request: VideoRequest, out_path: Path) -> VideoResult:
        if request.image is None or not request.image.is_file():
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, "image_to_video needs an existing image", penalize=False)
        width, height = request.width // 2 * 2, request.height // 2 * 2
        frames = max(2, int(round(request.duration * request.fps)))
        move, approximated = resolve_move(request.motion.camera)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        await ffmpeg(
            [
                "-i", request.image,
                "-vf", motion_filter(move, request.motion.intensity, width, height, frames, request.fps),
                "-frames:v", str(frames), "-r", str(request.fps), *VIDEO_ENCODE, *FASTSTART, "-an", out_path,
            ],
            timeout=max(300.0, request.duration * 60),
        )  # fmt: skip
        notes = ["camera motion only: subjects and environment stay still"]
        if approximated:
            notes.append(f"'{request.motion.camera}' approximated as '{move}'")
        return VideoResult(
            path=out_path, provider=self.name, model=self.model, duration=frames / request.fps, generative=False,
            metadata={"camera": move, "approximated": approximated, "notes": notes},
        )  # fmt: skip
