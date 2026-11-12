"""FFmpeg video editor: per-scene segments, cross-fades, music ducking, loudness, subtitles, logo."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import MediaError
from ..media.ffmpeg import AUDIO_ENCODE, FASTSTART, VIDEO_ENCODE, escape_filter_path, ffmpeg
from ..media.probe import MediaInfo, probe

TRANSITION_SECONDS = 0.35


@dataclass
class SegmentSpec:
    scene_id: str
    video: Path
    voice: Path | None = None
    narration: str = ""


@dataclass
class Segment:
    scene_id: str
    path: Path
    duration: float
    voice_duration: float
    narration: str


def scene_starts(durations: list[float], transition: float) -> list[float]:
    """Start time of each segment in the joined video (each cross-fade overlaps the next segment by ``transition``)."""
    starts, cursor = [], 0.0
    for index, duration in enumerate(durations):
        starts.append(round(cursor, 3))
        cursor += duration - (transition if index < len(durations) - 1 else 0.0)
    return starts


async def build_segment(spec: SegmentSpec, out: Path, *, width: int, height: int, fps: int, tail_pad: float = 0.25) -> Segment:
    """Normalise one scene: exact size/fps/codecs, audio laid over the picture, video extended if the voice is longer."""
    video_info = await probe(spec.video)
    voice_info = await probe(spec.voice) if spec.voice else None
    voice_duration = voice_info.duration if voice_info else 0.0
    duration = max(video_info.duration, voice_duration + tail_pad)
    extend = max(0.0, duration - video_info.duration)
    vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},setsar=1,fps={fps},format=yuv420p"
          + (f",tpad=stop_mode=clone:stop_duration={extend:.3f}" if extend > 0.02 else ""))  # fmt: skip
    args: list[str | Path] = ["-i", spec.video]
    if spec.voice:
        args += ["-i", spec.voice]
        af = f"[1:a]aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={duration:.3f},atrim=0:{duration:.3f},asetpts=N/SR/TB[a]"
    else:
        args += ["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        af = "[1:a]asetpts=N/SR/TB[a]"
    out.parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg([*args, "-filter_complex", f"[0:v]{vf}[v];{af}", "-map", "[v]", "-map", "[a]", "-t", f"{duration:.3f}",
                  *VIDEO_ENCODE, "-r", str(fps), *AUDIO_ENCODE, out], timeout=600)  # fmt: skip
    return Segment(spec.scene_id, out, duration, voice_duration, spec.narration)


async def join_segments(segments: list[Segment], out: Path, *, transition: float, fps: int) -> float:
    """Concatenate with cross-fades. Returns the duration of the joined video."""
    if not segments:
        raise MediaError("there are no scene segments to join")
    out.parent.mkdir(parents=True, exist_ok=True)
    if len(segments) == 1:
        shutil.copy2(segments[0].path, out)
        return segments[0].duration
    inputs: list[str | Path] = []
    for segment in segments:
        inputs += ["-i", segment.path]
    if transition <= 0:
        labels = "".join(f"[{i}:v][{i}:a]" for i in range(len(segments)))
        graph = f"{labels}concat=n={len(segments)}:v=1:a=1[v][a]"
        total = sum(s.duration for s in segments)
    else:
        parts: list[str] = []
        accumulated = segments[0].duration
        vlabel, alabel = "0:v", "0:a"
        for index in range(1, len(segments)):
            offset = accumulated - transition
            nv, na = f"v{index}", f"a{index}"
            parts.append(f"[{vlabel}][{index}:v]xfade=transition=fade:duration={transition}:offset={offset:.3f}[{nv}]")
            parts.append(f"[{alabel}][{index}:a]acrossfade=d={transition}[{na}]")
            vlabel, alabel = nv, na
            accumulated = offset + segments[index].duration
        graph = ";".join(parts) + f";[{vlabel}]null[v];[{alabel}]anull[a]"
        total = accumulated
    await ffmpeg([*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", *VIDEO_ENCODE, "-r", str(fps), *AUDIO_ENCODE, out],
                 timeout=1800)  # fmt: skip
    return total


async def finalize(
    joined: Path, out: Path, *, width: int, height: int, duration: float, subtitles_ass: Path | None = None, music: Path | None = None,
    watermark: Path | None = None, intro: Path | None = None, outro: Path | None = None, fps: int = 30,
) -> MediaInfo:  # fmt: skip
    """Final pass: optional music bed ducked under the voice, loudness normalisation, logo and burned-in subtitles."""
    inputs: list[str | Path] = ["-i", joined]
    next_input = 1
    music_index = watermark_index = None
    if music is not None:
        inputs += ["-stream_loop", "-1", "-i", music]
        music_index, next_input = next_input, next_input + 1
    if watermark is not None:
        inputs += ["-i", watermark]
        watermark_index, next_input = next_input, next_input + 1

    video = "[0:v]"
    chain: list[str] = []
    if watermark_index is not None:
        chain.append(f"[{watermark_index}:v]scale=iw*min(1\\,{int(width * 0.16)}/iw):-1,format=rgba[wm]")
        chain.append(f"{video}[wm]overlay=W-w-{int(width * 0.04)}:{int(height * 0.04)}[vw]")
        video = "[vw]"
    if subtitles_ass is not None:
        chain.append(f"{video}subtitles=filename='{escape_filter_path(subtitles_ass)}'[vs]")
        video = "[vs]"
    chain.append(f"{video}null[vout]")

    if music_index is not None:
        fade_start = max(0.0, duration - 1.5)
        chain.append(f"[{music_index}:a]aresample=48000,volume=0.30,atrim=0:{duration:.3f},afade=t=out:st={fade_start:.3f}:d=1.5[bed]")
        chain.append("[0:a]asplit=2[voice][key]")
        chain.append("[bed][key]sidechaincompress=threshold=0.04:ratio=9:attack=15:release=450[ducked]")
        chain.append("[voice][ducked]amix=inputs=2:normalize=0:duration=first[mix]")
        audio = "[mix]"
    else:
        audio = "[0:a]"
    chain.append(f"{audio}loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[aout]")
    out.parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg([*inputs, "-filter_complex", ";".join(chain), "-map", "[vout]", "-map", "[aout]", "-t", f"{duration:.3f}",
                  *VIDEO_ENCODE, "-r", str(fps), *AUDIO_ENCODE, *FASTSTART, out], timeout=1800)  # fmt: skip
    if intro is not None or outro is not None:
        pieces = [p for p in (intro, out, outro) if p is not None]
        stitched = out.with_suffix(".stitched.mp4")
        segs = [Segment(f"p{i}", p, (await probe(p)).duration, 0.0, "") for i, p in enumerate(pieces)]
        normalised = []
        for index, seg in enumerate(segs):
            target = out.with_suffix(f".part{index}.mp4")
            normalised.append(await build_segment(SegmentSpec(seg.scene_id, seg.path), target, width=width, height=height, fps=fps))
        await join_segments(normalised, stitched, transition=0.0, fps=fps)
        for part in normalised:
            part.path.unlink(missing_ok=True)
        stitched.replace(out)
    return await probe(out)


async def extract_frame(video: Path, at: float, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg(["-ss", f"{max(0.0, at):.3f}", "-i", video, "-frames:v", "1", "-q:v", "2", out], timeout=120)
    if not out.is_file() or out.stat().st_size == 0:
        raise MediaError(f"could not extract a frame at {at:.2f}s from {video.name}")
    return out


async def mix_ambient(out: Path, duration: float, *, base_hz: float = 110.0) -> Path:
    """A procedural ambient bed (soft pads + filtered noise). Original by construction: no samples, no licensing."""
    expr = (f"sine=f={base_hz}:d={duration:.2f},volume=0.35[a];sine=f={base_hz * 1.5}:d={duration:.2f},volume=0.22[b];"
            f"sine=f={base_hz * 2.003}:d={duration:.2f},volume=0.15[c];anoisesrc=color=pink:d={duration:.2f}:amplitude=0.04,lowpass=f=700[n];"
            f"[a][b][c][n]amix=inputs=4:normalize=0,tremolo=f=0.18:d=0.35,afade=t=in:d=2,afade=t=out:st={max(0.0, duration - 2):.2f}:d=2,"
            "aformat=channel_layouts=stereo:sample_rates=48000[out]")  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    await ffmpeg(["-filter_complex", expr, "-map", "[out]", "-c:a", "pcm_s16le", out], timeout=300)
    return out
