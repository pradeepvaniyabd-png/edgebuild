"""Microsoft Edge online voices through the community ``edge-tts`` package.

Opt-in (``SAX_EDGE_TTS_ENABLED=true`` and ``pip install 'super-agent-x[edge-tts]'``):
it sends the text to a Microsoft endpoint that is not an official public API, so it
can change or rate-limit without notice. Natural Hindi and Indian-English voices.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from ...errors import ProviderError
from ...lang import has_devanagari
from ...types import ProviderStatus, ProviderTier
from ..base import Readiness, VoiceInfo, VoiceProvider, VoiceRequest, VoiceResult

_DEFAULTS = {"hi": "hi-IN-SwaraNeural", "en": "en-IN-NeerjaNeural"}


class EdgeTTSVoice(VoiceProvider):
    name = "edge-tts"
    tier = ProviderTier.FREE
    model = "edge-neural"
    description = "Microsoft Edge neural voices (online, unofficial endpoint, opt-in)"

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def readiness(self) -> Readiness:
        if not self.enabled:
            return Readiness.not_configured("disabled (set SAX_EDGE_TTS_ENABLED=true to opt in)")
        if importlib.util.find_spec("edge_tts") is None:
            return Readiness.unavailable("the 'edge-tts' package is not installed (pip install 'super-agent-x[edge-tts]')")
        return Readiness.ready()

    def default_voice(self, language: str) -> str | None:
        return _DEFAULTS["hi" if language.lower().startswith("hi") else "en"]

    async def list_voices(self) -> list[VoiceInfo]:
        import edge_tts

        try:
            voices = await edge_tts.list_voices()
        except Exception as exc:
            raise ProviderError(self.name, ProviderStatus.NETWORK_ERROR, f"could not list voices: {exc}") from exc
        return [
            VoiceInfo(id=v["ShortName"], name=v.get("FriendlyName", v["ShortName"]), language=v.get("Locale", ""),
                      gender=v.get("Gender", ""))
            for v in voices
        ]  # fmt: skip

    async def text_to_speech(self, request: VoiceRequest, out_path: Path) -> VoiceResult:
        import edge_tts

        language = "hi" if has_devanagari(request.text) else request.language
        voice = request.voice or self.default_voice(language) or _DEFAULTS["en"]
        rate = f"{int(round((request.speed - 1.0) * 100)):+d}%"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        target = out_path if out_path.suffix.lower() == ".mp3" else out_path.with_name(out_path.stem + ".tts.mp3")
        try:
            await edge_tts.Communicate(request.text, voice, rate=rate).save(str(target))
        except Exception as exc:  # the package raises several transport-specific errors
            target.unlink(missing_ok=True)
            raise ProviderError(self.name, ProviderStatus.NETWORK_ERROR, f"synthesis failed: {exc}") from exc
        if not target.is_file() or target.stat().st_size < 200:
            target.unlink(missing_ok=True)
            raise ProviderError(self.name, ProviderStatus.TEMPORARY_ERROR, "no audio was returned")
        if target != out_path:
            from ...media.ffmpeg import ffmpeg

            await ffmpeg(["-i", target, out_path], timeout=120)
            target.unlink(missing_ok=True)
        return VoiceResult(path=out_path, provider=self.name, voice=voice)
