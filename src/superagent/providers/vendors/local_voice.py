"""Offline text-to-speech: Piper (neural, natural) and espeak-ng (formant, robotic but everywhere)."""

from __future__ import annotations

from pathlib import Path

from ...errors import ProviderError
from ...lang import has_devanagari
from ...media.ffmpeg import ffmpeg
from ...proc import run_process, which
from ...types import ProviderStatus, ProviderTier
from ..base import Readiness, VoiceInfo, VoiceProvider, VoiceRequest, VoiceResult

_MIN_WAV_BYTES = 200  # a header-only WAV means the engine produced no speech


async def _finalise(name: str, wav: Path, out_path: Path) -> None:
    if not wav.is_file() or wav.stat().st_size < _MIN_WAV_BYTES:
        wav.unlink(missing_ok=True)
        raise ProviderError(name, ProviderStatus.UNAVAILABLE, "the engine produced no audio for this text", penalize=False)
    if wav != out_path:
        await ffmpeg(["-i", wav, out_path], timeout=120)
        wav.unlink(missing_ok=True)


def _wav_target(out_path: Path) -> Path:
    return out_path if out_path.suffix.lower() == ".wav" else out_path.with_name(out_path.stem + ".tts.wav")


class EspeakVoice(VoiceProvider):
    name = "espeak-ng"
    tier = ProviderTier.LOCAL_FALLBACK
    model = "espeak-ng"
    description = "offline formant synthesis: instant and free, clearly robotic"

    def __init__(self, binary: str = "espeak-ng"):
        self.binary = binary

    def readiness(self) -> Readiness:
        if which(self.binary) is None:
            return Readiness.not_configured("espeak-ng is not installed (apt install espeak-ng)")
        return Readiness.ready()

    def default_voice(self, language: str) -> str | None:
        return "hi" if language.lower().startswith("hi") else "en-us"

    async def list_voices(self) -> list[VoiceInfo]:
        result = await run_process([self.binary, "--voices"], timeout=20)
        voices: list[VoiceInfo] = []
        for line in result.stdout.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 4:
                gender = parts[2].split("/")[-1] if "/" in parts[2] else ""
                voices.append(VoiceInfo(id=parts[1], name=parts[3], language=parts[1], gender=gender))
        return voices

    async def text_to_speech(self, request: VoiceRequest, out_path: Path) -> VoiceResult:
        language = "hi" if has_devanagari(request.text) else request.language
        voice = request.voice or self.default_voice(language) or "en-us"
        wav = _wav_target(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        words_per_minute = str(int(max(80, min(450, 160 * request.speed))))
        # Text goes through stdin so it can never be parsed as an option.
        result = await run_process(
            [self.binary, "-v", voice, "-s", words_per_minute, "-w", str(wav), "--stdin"],
            stdin_data=request.text.encode("utf-8"), timeout=180,
        )  # fmt: skip
        if not result.ok:
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, f"espeak-ng failed: {result.tail(200)}", penalize=False)
        await _finalise(self.name, wav, out_path)
        return VoiceResult(path=out_path, provider=self.name, voice=voice)


class PiperVoice(VoiceProvider):
    name = "piper"
    tier = ProviderTier.LOCAL
    description = "offline neural speech (Piper)"

    def __init__(self, binary: str, model: Path | None):
        self.binary = binary
        self.model_path = model
        self.model = model.name if model else None

    def readiness(self) -> Readiness:
        if self.model_path is None:
            return Readiness.not_configured("no Piper voice model (set PIPER_MODEL to a .onnx file)")
        if not self.model_path.is_file():
            return Readiness.not_configured(f"Piper model not found: {self.model_path}")
        if which(self.binary) is None:
            return Readiness.not_configured(f"'{self.binary}' is not installed")
        return Readiness.ready()

    def default_voice(self, language: str) -> str | None:
        return self.model

    async def list_voices(self) -> list[VoiceInfo]:
        return [VoiceInfo(id=self.model, name=self.model)] if self.model else []

    async def text_to_speech(self, request: VoiceRequest, out_path: Path) -> VoiceResult:
        wav = _wav_target(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        result = await run_process(
            [self.binary, "--model", str(self.model_path), "--output_file", str(wav),
             "--length_scale", f"{1.0 / request.speed:.3f}"],
            stdin_data=request.text.encode("utf-8"), timeout=600,
        )  # fmt: skip
        if not result.ok:
            raise ProviderError(self.name, ProviderStatus.UNAVAILABLE, f"piper failed: {result.tail(200)}", penalize=False)
        await _finalise(self.name, wav, out_path)
        return VoiceResult(path=out_path, provider=self.name, voice=self.model)
