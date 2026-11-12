"""Intent engine: what does the user want, in which language, and how big is the job?

Rules first (fast, offline, deterministic, covers English, Hindi and Hinglish). A
language model is consulted only when the rules find nothing to hold on to.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..lang import detect_language
from ..observability import get_logger
from ..types import Intent, ProviderKind

log = get_logger("intent")

# keyword → weight. Latin keywords match on word boundaries; Devanagari ones as substrings.
_LEXICON: dict[Intent, dict[str, float]] = {
    Intent.CODING: {
        "code": 2, "coding": 2, "implement": 2, "refactor": 2, "function": 1, "class": 1, "api": 1, "endpoint": 1,
        "python": 1, "javascript": 1, "typescript": 1, "react": 1, "fastapi": 1, "next.js": 1, "nextjs": 1,
        "repo": 1.5, "repository": 1.5, "unit test": 2, "tests likho": 2, "program": 1.5, "feature add": 2,
        "कोड": 2, "प्रोग्राम": 2,
    },  # fmt: skip
    Intent.DEBUGGING: {
        "debug": 2, "error": 1.5, "traceback": 2, "exception": 2, "crash": 2, "not working": 2, "failing": 1.5,
        "bug": 2, "fix karo": 2, "theek karo": 1.5, "kaam nahi kar raha": 2, "stack trace": 2, "एरर": 2, "त्रुटि": 2,
    },  # fmt: skip
    Intent.RESEARCH: {
        "research": 2, "find out": 1.5, "search": 1.5, "latest": 1, "compare": 1.5, "sources": 1.5, "khojo": 2,
        "pata karo": 2, "jankari": 1.5, "look up": 1.5, "news": 1, "खोज": 2, "शोध": 2, "जानकारी": 1.5,
    },  # fmt: skip
    Intent.FILE: {
        "pdf": 2, "docx": 2, "document": 1.5, "extract": 1.5, "summarize": 1, "summarise": 1, "summary": 1,
        "convert": 1.5, "saransh": 2, "file padho": 2, "दस्तावेज़": 2, "फाइल": 1.5, "फ़ाइल": 1.5, "सारांश": 2,
    },  # fmt: skip
    Intent.DATA: {
        "excel": 2, "xlsx": 2, "csv": 2, "spreadsheet": 2, "sheet": 1, "analyze": 1.5, "analyse": 1.5, "analysis": 1.5,
        "duplicate": 2, "duplicates": 2, "pivot": 2, "group by": 2, "aggregate": 1.5, "filter": 1, "data": 1,
        "एक्सेल": 2, "विश्लेषण": 2, "डेटा": 1.5, "डुप्लिकेट": 2,
    },  # fmt: skip
    Intent.REPORT: {"report": 2, "prativedan": 2, "प्रतिवेदन": 2, "रिपोर्ट": 2},
    Intent.GOVERNMENT: {
        "patra": 2, "goswara": 2.5, "panchayat": 2, "janpad": 2, "sambal": 2.5, "samagra": 2.5, "pension": 1.5,
        "cm helpline": 2.5, "ekyc": 2, "e-kyc": 2, "aadesh": 2, "suchna": 1.5, "sarkari": 2, "official letter": 2.5,
        "smaran patra": 2.5, "karyalay": 1.5, "पत्र": 2, "गोसवारा": 2.5, "पंचायत": 2, "जनपद": 2, "संबल": 2.5,
        "समग्र": 2.5, "पेंशन": 2, "हेल्पलाइन": 2, "केवाईसी": 2, "आदेश": 2, "शासकीय": 2, "कार्यालय": 1.5,
    },  # fmt: skip
    Intent.TRADING: {
        "trading": 2, "trade": 1.5, "xau": 2.5, "xauusd": 2.5, "gold chart": 2, "btc": 2, "btcusd": 2.5, "bitcoin": 1.5,
        "forex": 2, "smc": 2.5, "ict": 2, "order block": 2.5, "fvg": 2.5, "bos": 2, "choch": 2.5, "liquidity": 1.5,
        "candle": 1.5, "ohlc": 2.5, "backtest": 2.5, "risk reward": 2, "position size": 2.5, "dxy": 2.5, "ema": 1.5,
        "stop loss": 2, "lot size": 2,
    },  # fmt: skip
    Intent.IMAGE: {
        "image": 2, "picture": 2, "photo": 1.5, "poster": 2, "illustration": 2, "logo": 1.5, "wallpaper": 2,
        "tasveer": 2, "photo banao": 2, "चित्र": 2, "तस्वीर": 2, "इमेज": 2, "फोटो": 1.5,
    },  # fmt: skip
    Intent.VIDEO: {
        "video": 2.5, "reel": 2.5, "reels": 2.5, "shorts": 2, "short film": 2.5, "clip": 1.5, "animation": 2,
        "animated": 2, "film": 1.5, "वीडियो": 2.5, "रील": 2.5, "विडियो": 2.5,
    },  # fmt: skip
    Intent.AUDIO: {
        "audio": 2, "music": 2, "sfx": 2.5, "sound effect": 2.5, "podcast": 2, "mix audio": 2, "ऑडियो": 2, "संगीत": 2,
    },  # fmt: skip
    Intent.VOICE: {
        "voiceover": 2.5, "voice over": 2.5, "narration": 2, "tts": 2.5, "text to speech": 2.5, "awaaz": 2,
        "bolkar": 1.5, "आवाज़": 2, "आवाज": 2,
    },  # fmt: skip
    Intent.CONTENT: {
        "youtube": 2, "instagram": 2, "caption": 2, "hashtag": 2, "hashtags": 2, "social media": 2, "thumbnail": 2,
        "content": 1.5, "post": 1, "title and description": 2.5, "viral": 1.5, "कैप्शन": 2,
    },  # fmt: skip
    Intent.AUTOMATION: {
        "schedule": 2.5, "every day": 2.5, "everyday": 2.5, "daily": 2, "weekly": 2, "every hour": 2.5, "cron": 2.5,
        "automate": 2, "recurring": 2, "har din": 2.5, "roz": 2, "har roz": 2.5, "हर दिन": 2.5, "रोज़": 2, "रोज": 2,
    },  # fmt: skip
    Intent.SYSTEM: {
        "doctor": 2.5, "health check": 2.5, "system status": 2.5, "providers": 2, "provider status": 2.5,
        "which models": 2, "configuration": 1.5, "diagnostics": 2,
    },  # fmt: skip
}
_THRESHOLD = 1.5
_MEDIA = {Intent.IMAGE, Intent.VIDEO, Intent.AUDIO, Intent.VOICE}
_CAPABILITIES = {
    Intent.GENERAL: "general.answer", Intent.RESEARCH: "research.report", Intent.CODING: "code.implement",
    Intent.DEBUGGING: "debug.diagnose", Intent.FILE: "file.extract", Intent.DATA: "data.analyze",
    Intent.REPORT: "report.generate", Intent.GOVERNMENT: "government.documents", Intent.TRADING: "trading.analyze",
    Intent.IMAGE: "image.create", Intent.VIDEO: "video.create", Intent.AUDIO: "audio.mix", Intent.VOICE: "voice.speak",
    Intent.CONTENT: "content.social", Intent.AUTOMATION: "schedule.job", Intent.SYSTEM: "system.status",
    Intent.MULTIMODAL: "creative.pipeline",
}  # fmt: skip

_PATH = re.compile(
    r"""(?:"([^"\n]+\.[A-Za-z0-9]{2,5})"|'([^'\n]+\.[A-Za-z0-9]{2,5})'|((?:[~./\\]|[A-Za-z]:\\)?[\w./\\\-]+\.(?:xlsx|xlsm|csv|tsv|pdf|docx|txt|json|md|png|jpe?g|webp|mp4|mov|mp3|wav|py|js|ts|log|srt)))""",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"']+")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*[- ]?\s*(seconds?|secs?|sec|s|सेकंड|सेकेंड|minutes?|mins?|min|मिनट)\b", re.IGNORECASE)
_RATIO = re.compile(r"\b(9:16|16:9|1:1|4:5|4:3|3:4)\b")
_QUOTED = re.compile(r"[\"“]([^\"”\n]{3,})[\"”]")
_STYLES = {
    "paper pop-up book": ["pop-up book", "popup book", "pop up book", "paper pop"],
    "miniature diorama": ["miniature", "diorama", "tilt-shift", "tilt shift"],
    "clay animation": ["clay", "claymation"], "stylized 3D": ["3d", "pixar", "stylized"], "anime": ["anime"],
    "cartoon": ["cartoon"], "comic": ["comic"], "cinematic": ["cinematic", "filmy"], "photorealistic": ["photoreal", "realistic"],
    "documentary": ["documentary"], "commercial": ["commercial", "advert", "ad film"], "fantasy": ["fantasy"],
    "educational": ["educational", "explainer", "tutorial"],
}  # fmt: skip
_TONES = {"funny": ["funny", "comedy", "mazedar", "mazakiya", "हास्य", "मज़ेदार"], "emotional": ["emotional", "bhavuk", "भावुक"],
          "inspiring": ["motivational", "inspiring", "prerak", "प्रेरक"], "serious": ["serious", "gambhir", "गंभीर"]}  # fmt: skip
_VERTICAL = ("shorts", "reel", "reels", "tiktok", "vertical", "instagram", "रील")
_HORIZONTAL = ("landscape", "horizontal", "youtube video", "widescreen")


class IntentResult(BaseModel):
    intents: list[Intent]
    primary: Intent
    language: str = "en"
    complexity: Literal["simple", "moderate", "complex"] = "simple"
    capabilities: list[str] = Field(default_factory=list)
    entities: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    method: Literal["rules", "llm"] = "rules"
    scores: dict[str, float] = Field(default_factory=dict)

    def has(self, *intents: Intent) -> bool:
        return any(intent in self.intents for intent in intents)


class _LLMIntent(BaseModel):
    intents: list[str] = Field(min_length=1, max_length=6)


def _match(text: str, keyword: str) -> int:
    """Position of the keyword in ``text`` or -1. ASCII keywords need word boundaries."""
    if keyword.isascii():
        found = re.search(rf"(?<![A-Za-z0-9]){re.escape(keyword)}(?![A-Za-z0-9])", text)
        return found.start() if found else -1
    return text.find(keyword)


def extract_entities(command: str) -> dict[str, Any]:
    lowered = command.lower()
    entities: dict[str, Any] = {}
    urls = _URL.findall(command)
    without_urls = _URL.sub(" ", command)
    paths = [next(group for group in match if group) for match in _PATH.findall(without_urls)]
    if paths:
        entities["paths"] = list(dict.fromkeys(paths))
    if urls:
        entities["urls"] = [url.rstrip(".,)") for url in urls]
    duration = _DURATION.search(command)
    if duration:
        value, unit = float(duration.group(1)), duration.group(2).lower()
        entities["duration_seconds"] = value * 60 if unit.startswith(("min", "मिनट")) else value
    ratio = _RATIO.search(command)
    if ratio:
        entities["aspect_ratio"] = ratio.group(1)
    elif any(_match(lowered, word) >= 0 for word in _VERTICAL):
        entities["aspect_ratio"] = "9:16"
    elif any(word in lowered for word in _HORIZONTAL):
        entities["aspect_ratio"] = "16:9"
    for style, words in _STYLES.items():
        if any(_match(lowered, word) >= 0 for word in words):
            entities.setdefault("styles", []).append(style)
    for tone, words in _TONES.items():
        if any(_match(lowered, word) >= 0 for word in words):
            entities["tone"] = tone
            break
    quoted = _QUOTED.findall(command)
    if quoted:
        entities["quoted"] = quoted
    return entities


class IntentEngine:
    def __init__(self, ai: Any = None):
        self.ai = ai

    def classify(self, command: str) -> IntentResult:
        """Pure rule-based classification. Never touches the network."""
        lowered = command.lower()
        # File names and URLs are data, not vocabulary: "report.pdf" must not read as a report request.
        prose = _PATH.sub(lambda m: " " * len(m.group(0)), _URL.sub(lambda m: " " * len(m.group(0)), lowered))
        scores: dict[Intent, float] = {}
        first_seen: dict[Intent, int] = {}
        for intent, words in _LEXICON.items():
            for keyword, weight in words.items():
                position = _match(prose, keyword)
                if position >= 0:
                    scores[intent] = scores.get(intent, 0.0) + weight
                    first_seen[intent] = min(first_seen.get(intent, position), position)
        entities = extract_entities(command)

        # File types named in the request are strong evidence on their own.
        for path in entities.get("paths", []):
            suffix = path.rsplit(".", 1)[-1].lower()
            if suffix in {"xlsx", "xlsm", "csv", "tsv"}:
                scores[Intent.DATA] = scores.get(Intent.DATA, 0) + 2
                first_seen.setdefault(Intent.DATA, lowered.find(path.lower()))
            elif suffix in {"pdf", "docx", "txt", "md"}:
                scores[Intent.FILE] = scores.get(Intent.FILE, 0) + 2
                first_seen.setdefault(Intent.FILE, lowered.find(path.lower()))
            elif suffix in {"py", "js", "ts"}:
                scores[Intent.CODING] = scores.get(Intent.CODING, 0) + 1.5
                first_seen.setdefault(Intent.CODING, lowered.find(path.lower()))

        chosen = [intent for intent, score in scores.items() if score >= _THRESHOLD]
        # Disambiguation.
        if Intent.TRADING in chosen and Intent.DATA in chosen and scores[Intent.TRADING] >= scores[Intent.DATA]:
            chosen.remove(Intent.DATA)  # "analyze this XAUUSD csv" is a trading request about a data file
        if Intent.GOVERNMENT in chosen and Intent.FILE in chosen and not entities.get("paths"):
            chosen.remove(Intent.FILE)  # "patra" drafting is not document extraction
        if Intent.VIDEO in chosen and Intent.IMAGE in chosen and scores[Intent.IMAGE] < 2.5:
            chosen.remove(Intent.IMAGE)  # images are an internal step of making a video
        if Intent.CONTENT in chosen and Intent.VIDEO not in chosen and Intent.IMAGE not in chosen and "thumbnail" in lowered:
            chosen.append(Intent.IMAGE)
            first_seen.setdefault(Intent.IMAGE, lowered.find("thumbnail"))
        if Intent.SYSTEM in chosen and len(chosen) > 1 and scores[Intent.SYSTEM] < 2.5:
            chosen.remove(Intent.SYSTEM)
        if len([intent for intent in chosen if intent in _MEDIA]) >= 2 and Intent.MULTIMODAL not in chosen:
            chosen.append(Intent.MULTIMODAL)
            first_seen[Intent.MULTIMODAL] = len(lowered)

        chosen.sort(key=lambda intent: first_seen.get(intent, len(lowered)))
        if not chosen:
            chosen = [Intent.GENERAL]
            confidence = 0.3
        else:
            confidence = min(0.95, 0.5 + 0.1 * max(scores[i] for i in chosen if i in scores))
        primary = max(chosen, key=lambda intent: scores.get(intent, 0.0)) if chosen != [Intent.GENERAL] else Intent.GENERAL

        heavy = {Intent.VIDEO, Intent.MULTIMODAL, Intent.CODING}
        if len(chosen) >= 3 or any(intent in heavy for intent in chosen):
            complexity: Literal["simple", "moderate", "complex"] = "complex"
        elif len(chosen) == 2 or entities.get("paths") or len(command.split()) > 40:
            complexity = "moderate"
        else:
            complexity = "simple"
        return IntentResult(
            intents=chosen, primary=primary, language=detect_language(command), complexity=complexity,
            capabilities=[_CAPABILITIES[intent] for intent in chosen], entities=entities, confidence=round(confidence, 2),
            scores={intent.value: score for intent, score in sorted(scores.items(), key=lambda item: -item[1])},
        )  # fmt: skip

    async def detect(self, command: str) -> IntentResult:
        result = self.classify(command)
        if result.intents != [Intent.GENERAL] or self.ai is None or not self.ai.has(ProviderKind.LLM):
            return result
        if len(command.split()) < 4:
            return result  # greetings and one-liners are GENERAL; no model call needed
        allowed = [intent.value for intent in Intent]
        prompt = (
            f"Classify this request into one or more of these intents: {', '.join(allowed)}.\n"
            'Return {"intents": [...]} ordered by where each appears in the request. Use GENERAL only when '
            f"nothing else fits.\n\nRequest: {command}"
        )
        try:
            parsed = await self.ai.complete_json(prompt, _LLMIntent, max_tokens=300, attempts=1)
            intents = [Intent(value) for value in parsed.intents if value in allowed]
        except Exception:  # classification help is optional; the rule result stands
            log.info("LLM intent refinement unavailable; using rule-based result", exc_info=True)
            return result
        if intents and intents != [Intent.GENERAL]:
            result.intents = intents
            result.primary = intents[0]
            result.capabilities = [_CAPABILITIES[intent] for intent in intents]
            result.method = "llm"
            result.confidence = 0.7
            if len(intents) >= 3 or {Intent.VIDEO, Intent.MULTIMODAL, Intent.CODING} & set(intents):
                result.complexity = "complex"
        return result
