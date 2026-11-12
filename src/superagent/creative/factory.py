"""Autonomous video factory (spec §49): topic or category in, a complete content package out.

TOPIC → IDEA here, then the director's existing pipeline takes over:
SCRIPT → STORYBOARD → CHARACTERS → IMAGES → VIDEO → VOICE → MUSIC → SUBTITLES → THUMBNAIL → METADATA → QC → APPROVAL → EXPORT.
Nothing is published: export always waits for a human, as everywhere else.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from ..agents.base import AgentContext, AgentResult, BaseAgent, action
from ..core.dag import Task
from ..observability import get_logger
from ..types import PermissionLevel, ProviderKind

log = get_logger("factory")

CATEGORIES = ("motivation", "education", "kids", "tech", "comedy", "devotional", "news", "story")

_TRIGGER = re.compile(
    r"(?i)\b(?:content\s+(?:package|factory)|video\s+factory|daily\s+content|content\s+pack)\b"
    r"|(?:topic|category)\s*[:=]"
)
_CATEGORY = re.compile(r"(?i)\bcategory\s*[:=]?\s*([a-z]+)")
_NOISE = re.compile(
    r"(?i)\b(?:make|create|build|generate|prepare|banao|bana\s+do|taiyar\s+karo)\b"
    r"|\b(?:a|an|the|ek)\b(?=\s+(?:content|video|daily))"
    r"|\b(?:content\s+(?:package|factory|pack)|video\s+factory|daily\s+content)\b"
    r"|\b(?:topic|category)\s*[:=]?\s*(?:[a-z]+\s*[,;]\s*)?"
    r"|\b(?:on|about|par|pe|ke\s+upar)\b"
)

# Template ideas when no language model is configured. They are honest scaffolds, flagged as such.
_TEMPLATES: dict[str, dict[str, tuple[str, str, str]]] = {
    "en": {
        "motivation": ("{topic}: one small step", "someone facing {topic} takes one small, brave step and it changes their day", "uplifting"),
        "education": ("{topic} in 60 seconds", "a clear, visual explanation of {topic} from the basics to one surprising fact", "clear and curious"),
        "kids": ("The little story of {topic}", "a gentle children's story where a small hero learns something kind about {topic}", "warm and playful"),
        "tech": ("{topic}, simply explained", "what {topic} is, why it matters and one practical tip, shown with simple visuals", "confident"),
        "comedy": ("When {topic} goes wrong", "a short, light comedy where an everyday moment about {topic} goes hilariously wrong", "funny"),
        "devotional": ("{topic}: a quiet moment", "a calm, respectful reflection on {topic} with soft light and simple imagery", "calm"),
        "news": ("{topic}: what to know", "a neutral explainer of the key facts about {topic}, with no claims beyond the facts given", "neutral"),
        "story": ("{topic}", "a short story about {topic} with one character, one problem and a satisfying turn", "heartfelt"),
    },
    "hinglish": {
        "motivation": ("{topic}: ek chhota kadam", "{topic} ka saamna karta ek insaan ek chhota sa himmat wala kadam uthata hai aur din badal jata hai", "uplifting"),
        "education": ("{topic} 60 second mein", "{topic} ko basics se ek surprising fact tak, simple visuals ke saath samjhaana", "clear and curious"),
        "kids": ("{topic} ki chhoti si kahani", "bachchon ki ek pyaari kahani jismein ek chhota hero {topic} ke baare mein kuch achha seekhta hai", "warm and playful"),
        "tech": ("{topic} aasaan bhasha mein", "{topic} kya hai, kyun zaroori hai, aur ek kaam ki tip, simple visuals ke saath", "confident"),
        "comedy": ("Jab {topic} mein gadbad ho jaye", "{topic} se judi ek roz ki situation jo mazedaar tareeke se ulti pad jaati hai", "funny"),
        "devotional": ("{topic}: ek shaant pal", "{topic} par ek shaant, aadar bhara chintan, naram roshni aur saadi tasveeron ke saath", "calm"),
        "news": ("{topic}: zaroori baatein", "{topic} ke mukhya tathyon ki neutral jaankari, tathyon se aage koi daawa nahi", "neutral"),
        "story": ("{topic}", "{topic} par ek chhoti kahani: ek kirdaar, ek mushkil, aur ek achha mod", "heartfelt"),
    },
    "hi": {
        "motivation": ("{topic}: एक छोटा कदम", "{topic} का सामना करता एक व्यक्ति एक छोटा, साहसी कदम उठाता है और उसका दिन बदल जाता है", "uplifting"),
        "education": ("{topic} 60 सेकंड में", "{topic} को मूल बातों से एक रोचक तथ्य तक, सरल दृश्यों के साथ समझाना", "clear and curious"),
        "kids": ("{topic} की छोटी सी कहानी", "बच्चों की एक प्यारी कहानी जिसमें एक छोटा नायक {topic} के बारे में कुछ अच्छा सीखता है", "warm and playful"),
        "tech": ("{topic} सरल भाषा में", "{topic} क्या है, क्यों ज़रूरी है, और एक काम की सलाह, सरल दृश्यों के साथ", "confident"),
        "comedy": ("जब {topic} में गड़बड़ हो जाए", "{topic} से जुड़ी एक रोज़ की स्थिति जो मज़ेदार ढंग से उलटी पड़ जाती है", "funny"),
        "devotional": ("{topic}: एक शांत क्षण", "{topic} पर एक शांत, आदरपूर्ण चिंतन, कोमल रोशनी और सरल चित्रों के साथ", "calm"),
        "news": ("{topic}: ज़रूरी बातें", "{topic} के मुख्य तथ्यों की निष्पक्ष जानकारी, तथ्यों से आगे कोई दावा नहीं", "neutral"),
        "story": ("{topic}", "{topic} पर एक छोटी कहानी: एक पात्र, एक समस्या और एक सुखद मोड़", "heartfelt"),
    },
}
_DEFAULT_STYLE = {"kids": "paper pop-up book", "comedy": "cartoon", "education": "educational", "tech": "commercial",
                  "devotional": "cinematic", "news": "documentary", "motivation": "cinematic", "story": "cinematic"}  # fmt: skip


def wants_factory(command: str) -> bool:
    return bool(_TRIGGER.search(command))


def parse_request(command: str) -> tuple[str, str]:
    """(topic, category) from a free-form factory request."""
    match = _CATEGORY.search(command)
    category = match.group(1).lower() if match and match.group(1).lower() in CATEGORIES else ""
    if not category:
        category = next((c for c in CATEGORIES if re.search(rf"(?i)\b{c}\b", command)), "")
    topic = _NOISE.sub(" ", command)
    if category:
        topic = re.sub(rf"(?i)\b{category}\b", " ", topic)
    topic = re.sub(r"\s+", " ", topic).strip(" ,.:;-\"'")
    return topic, category


class ContentIdea(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    idea: str = Field(min_length=10, max_length=600, description="One or two sentences: who, what happens, why it is worth watching")
    hook: str = Field(default="", max_length=200, description="First-second hook line")
    tone: str = Field(default="", max_length=40)


class FactoryInput(BaseModel):
    topic: str = Field(min_length=2)
    category: str = ""
    language: str | None = None
    duration_seconds: float | None = None
    aspect_ratio: str = "9:16"
    style: str | None = None
    ambient_music: bool = True


def template_idea(topic: str, category: str, language: str) -> ContentIdea:
    table = _TEMPLATES.get(language, _TEMPLATES["en"])
    title, idea, tone = table.get(category or "story", table["story"])
    return ContentIdea(title=title.format(topic=topic)[:120], idea=idea.format(topic=topic), tone=tone)


class ContentFactoryAgent(BaseAgent):
    name = "factory"
    description = "Autonomous video factory: turns a topic or category into a complete, approval-gated content package."
    capabilities = ["creative.factory", "content.package"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("package", description="Topic/category → idea → full video package (script to QC); export waits for approval.", input=FactoryInput)
    async def package(self, ctx: AgentContext, args: FactoryInput) -> AgentResult:
        language = args.language or (ctx.language if ctx.language in {"en", "hi", "hinglish"} else "en")
        category = args.category.lower() if args.category.lower() in CATEGORIES else ""
        warnings: list[str] = []
        method = "template"
        idea = template_idea(args.topic, category, language)
        if ctx.ai is not None and ctx.ai.has(ProviderKind.LLM):
            prompt = (
                f"Propose one short-form video idea.\nTopic: {args.topic}\nCategory: {category or 'any fitting'}\n"
                f"Language of title and idea: {language}.\nRequirements: original (no existing franchises or living artists), "
                "one clear character or subject, one concrete situation, an emotional or informational payoff; "
                "factual topics must not invent facts."
            )
            try:
                idea = await ctx.ai.complete_json(prompt, ContentIdea, max_tokens=800, attempts=2)
                method = "llm"
            except Exception:  # fall back to the template rather than fail the package
                log.warning("idea generation failed; using the template idea", exc_info=True)
        if method == "template":
            warnings.append("no language model configured: the idea is a category template built around your topic")

        style = args.style or _DEFAULT_STYLE.get(category or "story", "cinematic")
        brief_input = {
            "idea": idea.idea if not idea.hook else f"{idea.idea}. Opening hook: {idea.hook}",
            "title": idea.title, "language": language, "duration_seconds": args.duration_seconds or 30.0,
            "aspect_ratio": args.aspect_ratio, "style": style, "tone": idea.tone, "ambient_music": args.ambient_music,
        }  # fmt: skip
        brief = Task(description=f"brief: {idea.title}", agent="director", action="brief", input=brief_input,
                     max_retries=ctx.settings.max_retries, timeout=ctx.settings.task_timeout)  # fmt: skip
        board = Task(description="story, bibles, shot list and production plan", agent="director", action="storyboard",
                     input={"project_id": f"${{{brief.id}.project_id}}"}, dependencies=[brief.id],
                     max_retries=ctx.settings.max_retries, timeout=ctx.settings.task_timeout)  # fmt: skip
        return AgentResult(
            output={"topic": args.topic, "category": category or "story", "title": idea.title, "idea": idea.idea, "hook": idea.hook,
                    "style": style, "language": language, "method": method},
            summary=f"content package started: “{idea.title}” ({category or 'story'}, {style}, {language})",
            warnings=warnings, spawn=[brief, board],
            verify=[{"type": "schema", "required": ["title", "idea"]}],
        )  # fmt: skip
