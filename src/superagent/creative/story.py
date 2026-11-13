"""StoryEngine, CharacterBible and StyleBible handling.

With a language model the story is written by the model and validated; without one a
plain three-act template is produced and flagged as such (it is structure, not writing).
"""

from __future__ import annotations

import re

from ..lang import detect_language
from ..observability import get_logger
from ..providers.services import AI
from ..types import ProviderKind
from ..utils import slugify, stable_hash
from .models import CharacterBible, CreativeBrief, Location, Scene, Story, StyleBible

log = get_logger("story")

STYLE_PRESETS: dict[str, dict[str, str]] = {
    "cinematic": {"rendering_style": "cinematic film still, anamorphic bokeh, fine film grain", "lighting": "motivated dramatic lighting",
                  "color_language": "teal and orange grade"},  # fmt: skip
    "photorealistic": {"rendering_style": "photorealistic, natural skin and material detail", "lighting": "soft natural daylight",
                       "color_language": "true-to-life colours"},  # fmt: skip
    "stylized 3D": {"rendering_style": "stylized 3D render, smooth shapes, subsurface scattering", "lighting": "soft studio key with rim light",
                    "color_language": "saturated friendly palette", "materials": "soft plastic and fabric"},  # fmt: skip
    "miniature diorama": {"rendering_style": "tilt-shift miniature diorama, tiny handcrafted set, macro photography",
                          "lighting": "warm practical lights", "color_language": "toy-like saturated colours",
                          "materials": "painted wood, felt, clay", "depth_of_field": "extreme tilt-shift blur"},  # fmt: skip
    "paper pop-up book": {"rendering_style": "paper pop-up book, layered cut-paper scenery, visible paper fibre and folds",
                          "lighting": "soft overhead light casting gentle paper shadows", "color_language": "bright craft-paper palette",
                          "materials": "cardstock, folded paper, glue tabs", "depth_of_field": "layered depth between paper planes"},  # fmt: skip
    "clay animation": {"rendering_style": "stop-motion clay animation, fingerprints in clay, handmade imperfections",
                       "lighting": "warm studio lighting", "color_language": "earthy saturated colours", "materials": "plasticine clay"},  # fmt: skip
    "cartoon": {"rendering_style": "clean 2D cartoon, bold outlines, flat colours", "lighting": "bright even lighting",
                "color_language": "vivid flat colours"},  # fmt: skip
    "anime": {"rendering_style": "anime key-frame illustration, cel shading", "lighting": "dramatic rim light",
              "color_language": "vibrant gradients"},  # fmt: skip
    "comic": {"rendering_style": "comic book panel, inked outlines, halftone shading", "lighting": "high contrast",
              "color_language": "limited bold palette"},  # fmt: skip
    "documentary": {"rendering_style": "documentary photography, handheld realism", "lighting": "available light",
                    "color_language": "naturalistic muted colours"},  # fmt: skip
    "commercial": {"rendering_style": "polished commercial product photography", "lighting": "clean studio lighting with soft reflections",
                   "color_language": "brand-friendly clean palette"},  # fmt: skip
    "fantasy": {"rendering_style": "painterly fantasy illustration, rich atmosphere", "lighting": "magical glowing light",
                "color_language": "jewel tones"},  # fmt: skip
    "educational": {"rendering_style": "clear flat explainer illustration", "lighting": "bright even lighting",
                    "color_language": "friendly limited palette", "composition": "uncluttered, one idea per frame"},  # fmt: skip
}
_ALIASES = {"pop-up book": "paper pop-up book", "popup": "paper pop-up book", "3d": "stylized 3D", "pixar": "stylized 3D",
            "tilt-shift": "miniature diorama", "clay": "clay animation", "realistic": "photorealistic", "explainer": "educational",
            "filmy": "cinematic", "advert": "commercial"}  # fmt: skip
_ARTIST_REFERENCE = re.compile(r"\b(?:in the style of|inspired by|like|à la)\s+[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,3}", re.UNICODE)

_BEATS = {
    "en": [("Setup", "{subject} appears in a calm, familiar place.", "Meet {subject}. Everything begins quietly."),
           ("Spark", "Something unexpected happens around {subject}.", "Then, something unexpected changes everything."),
           ("Trouble", "{subject} faces a small problem and tries a first fix.", "A small problem gets in the way."),
           ("Twist", "A surprising turn makes {subject} laugh or gasp.", "And then comes a surprising twist."),
           ("Resolution", "{subject} solves it in a clever way.", "With a clever idea, it all works out."),
           ("Ending", "{subject} smiles at the camera as the scene settles.", "And that is how the day ends.")],
    "hi": [("आरंभ", "{subject} एक शांत, जाने-पहचाने स्थान पर दिखाई देता है।", "मिलिए {subject} से। सब कुछ शांति से शुरू होता है।"),
           ("मोड़", "{subject} के आसपास कुछ अनपेक्षित घटित होता है।", "तभी कुछ अनपेक्षित हो जाता है।"),
           ("उलझन", "{subject} के सामने एक छोटी-सी समस्या आती है।", "एक छोटी-सी समस्या रास्ता रोक लेती है।"),
           ("रहस्य", "एक अचरज भरा मोड़ {subject} को हँसा देता है।", "और फिर आता है एक मज़ेदार मोड़।"),
           ("समाधान", "{subject} चतुराई से समस्या का हल निकालता है।", "एक समझदारी भरे विचार से सब ठीक हो जाता है।"),
           ("अंत", "{subject} कैमरे की ओर मुस्कुराता है।", "और इस तरह दिन का अंत होता है।")],
    "hinglish": [("Shuruaat", "{subject} ek shaant, jaani-pehchaani jagah par dikhta hai.", "Milte hain {subject} se. Sab kuch shaanti se shuru hota hai."),
                 ("Mod", "{subject} ke aaspaas kuch anjaana ho jaata hai.", "Tabhi kuch anjaana ho jaata hai."),
                 ("Uljhan", "{subject} ke saamne ek chhoti si dikkat aati hai.", "Ek chhoti si dikkat raasta rok leti hai."),
                 ("Twist", "Ek mazedaar twist {subject} ko hansa deta hai.", "Aur phir aata hai ek mazedaar twist."),
                 ("Hal", "{subject} samajhdaari se dikkat ka hal nikaalta hai.", "Ek smart idea se sab theek ho jaata hai."),
                 ("Ant", "{subject} camera ki taraf muskurata hai.", "Aur is tarah din ka ant hota hai.")],
}  # fmt: skip
_CAMERAS = ["push-in", "pan-right", "pull-out", "tilt-up", "pan-left", "push-in"]


def resolve_style(name: str) -> tuple[str, StyleBible]:
    """Map free text to a known style and build its bible. Strips 'in the style of <name>' phrases."""
    cleaned = _ARTIST_REFERENCE.sub("", name or "").strip() or "cinematic"
    lowered = cleaned.lower()
    key = next((k for k in STYLE_PRESETS if k.lower() in lowered), None) or next((v for a, v in _ALIASES.items() if a in lowered), "cinematic")
    return key, StyleBible(visual_style=key, **STYLE_PRESETS[key])


def scene_count(duration: float, target_scene_seconds: float = 8.0) -> int:
    return max(3, min(12, round(duration / target_scene_seconds)))


def compose_image_prompt(scene: Scene, story: Story, style: StyleBible) -> str:
    present = [c for c in story.characters if c.character_id in scene.characters or c.name in scene.characters]
    location = next((loc for loc in story.locations if loc.location_id == scene.location or loc.name == scene.location), None)
    parts = [scene.action.strip().rstrip(".")]
    if location:
        parts.append(f"setting: {location.description}")
    elif scene.environment:
        parts.append(f"setting: {scene.environment}")
    parts += [c.prompt_fragment() for c in present]
    if scene.emotion:
        parts.append(f"mood: {scene.emotion}")
    parts.append(f"{scene.lens} lens, {scene.time}")
    parts.append(scene.lighting or style.lighting)
    parts.append(style.prompt_fragment())
    parts.append(f"vertical composition {style.aspect_ratio}, no text or captions in the image")
    return ". ".join(p.strip() for p in parts if p and p.strip())


def compose_video_prompt(scene: Scene, style: StyleBible) -> str:
    moves = {"push-in": "slow push-in", "pull-out": "slow pull-out", "pan-left": "gentle pan left", "pan-right": "gentle pan right",
             "tilt-up": "slow tilt up", "tilt-down": "slow tilt down", "handheld": "subtle handheld"}  # fmt: skip
    return f"{scene.action.strip().rstrip('.')}. {moves.get(scene.camera, scene.camera)}, natural subject motion, {style.visual_style}, consistent look"


def finalize(story: Story, brief: CreativeBrief, style: StyleBible) -> Story:
    """Normalise a story from any source: unique scene ids, durations adding up, prompts filled in."""
    seen: set[str] = set()
    for index, scene in enumerate(story.scenes, 1):
        scene.scene_id = re.sub(r"[^a-zA-Z0-9_-]", "", scene.scene_id or "") or f"s{index:02d}"
        if scene.scene_id in seen:
            scene.scene_id = f"{scene.scene_id}-{index}"
        seen.add(scene.scene_id)
    total = sum(scene.duration for scene in story.scenes)
    if total > 0 and abs(total - brief.duration_seconds) > 0.5:
        factor = brief.duration_seconds / total
        for scene in story.scenes:
            scene.duration = round(max(2.0, min(20.0, scene.duration * factor)), 2)
    story.duration = round(sum(scene.duration for scene in story.scenes), 2)
    story.aspect_ratio, story.language = brief.aspect_ratio, brief.language
    known = {c.character_id for c in story.characters} | {c.name for c in story.characters}
    for character in story.characters:
        character.character_id = slugify(character.character_id or character.name, 24)
        character.seed = character.seed or int(stable_hash(character.character_id, 8), 16) % 2_000_000_000
    for scene in story.scenes:
        scene.characters = [c for c in scene.characters if c in known]
        scene.negative_prompt = scene.negative_prompt or style.negative
        scene.image_prompt = scene.image_prompt or compose_image_prompt(scene, story, style)
        scene.video_prompt = scene.video_prompt or compose_video_prompt(scene, style)
    return story


class StoryEngine:
    def __init__(self, ai: AI | None):
        self.ai = ai

    @property
    def can_write(self) -> bool:
        return self.ai is not None and self.ai.has(ProviderKind.LLM)

    async def create(self, brief: CreativeBrief, style: StyleBible) -> Story:
        if self.can_write:
            try:
                return await self._write(brief, style)
            except Exception:  # fall back rather than fail the project on a bad model reply
                log.warning("model-written story failed validation; using the template story", exc_info=True)
        return self.template(brief, style)

    async def _write(self, brief: CreativeBrief, style: StyleBible) -> Story:
        count = scene_count(brief.duration_seconds)
        language = {"hi": "Hindi (Devanagari)", "hinglish": "Hinglish (Hindi in Roman script)", "en": "English"}.get(brief.language, brief.language)
        prompt = (
            f"Write a short video story.\nIdea: {brief.idea}\nTitle: {brief.title or '(choose one)'}\n"
            f"Total duration: {brief.duration_seconds:g} seconds in exactly {count} scenes of roughly equal length.\n"
            f"Narration language: {language}. Tone: {brief.tone or 'fitting the idea'}. Audience: {brief.audience or 'general'}.\n"
            f"Visual style (already decided): {style.visual_style}. Aspect ratio {brief.aspect_ratio}.\n\n"
            "Requirements:\n- Define 1-3 recurring characters with a precise visual description (face, hair, clothing, colours) so they "
            "can be drawn identically in every scene.\n- Every scene has one concrete visual action, a location, a camera move "
            "(push-in, pull-out, pan-left, pan-right, tilt-up, tilt-down or handheld), emotion, narration of 12-25 words that can "
            "be spoken in about the scene's duration, and optional dialogue lines.\n- Set characters by their character_id.\n"
            "- Original characters and worlds only: do not imitate living artists or existing franchises.\n"
            "- Leave image_prompt and video_prompt empty; they are composed later."
        )
        story = await self.ai.complete_json(prompt, Story, max_tokens=12000, attempts=2)  # type: ignore[union-attr]
        story.method = "llm"
        return finalize(story, brief, style)

    def template(self, brief: CreativeBrief, style: StyleBible) -> Story:
        language = brief.language if brief.language in _BEATS else "en"
        subject = re.sub(r"\s+", " ", brief.idea).strip().rstrip(".")[:80]
        title = brief.title or subject[:60]
        hero = CharacterBible(
            character_id="hero", name={"hi": "नायक", "hinglish": "Hero"}.get(language, "Hero"),
            visual_description=f"main character of the story about {subject}", body_proportions="friendly proportions",
            clothing="simple distinctive outfit in one signature colour", colors=["warm orange", "cream"],
            expressions="expressive, readable emotions", personality="curious and kind",
        )  # fmt: skip
        place = Location(location_id="main", name="Main place", description=f"the main setting of '{subject}', rendered in the {style.visual_style} look")
        count = scene_count(brief.duration_seconds)
        beats = _BEATS[language]
        per_scene = round(brief.duration_seconds / count, 2)
        scenes = []
        for index in range(count):
            label, action, narration = beats[min(len(beats) - 1, round(index * (len(beats) - 1) / max(1, count - 1)))]
            scenes.append(Scene(
                scene_id=f"s{index + 1:02d}", duration=per_scene, location="main", characters=["hero"],
                action=action.format(subject=subject), emotion=label.lower(), camera=_CAMERAS[index % len(_CAMERAS)],
                narration=narration.format(subject=subject),
            ))  # fmt: skip
        story = Story(title=title, genre=brief.tone or "short", duration=brief.duration_seconds, language=brief.language,
                      aspect_ratio=brief.aspect_ratio, synopsis=f"A short video about {subject}.", characters=[hero],
                      locations=[place], scenes=scenes, method="template")  # fmt: skip
        return finalize(story, brief, style)


_DIRECTIVES = [
    re.compile(r"(?i)\(\s*make (?:a|an|the)? ?(?:short )?(?:video|film|reel|movie)\s*\)"),
    re.compile(r"(?i)\b\d+(?:\.\d+)?\s*[-]?\s*(?:seconds?|secs?|sec|s|minutes?|mins?|min|सेकंड|सेकण्ड|मिनट)\b"),
    re.compile(r"(?i)\b(?:in|mein|me|में)\s+(?:hinglish|hindi|english|हिंदी|हिन्दी)\b"),
    re.compile(r"(?i)(?:hinglish|hindi|english|हिंदी|हिन्दी|अंग्रेज़ी|अंग्रेजी)\s+(?:mein|me|में|language)(?!\w)"),
    re.compile(r"(?i)\b(?:9:16|16:9|1:1|4:5|vertical|horizontal|portrait|landscape)\b"),
    re.compile(r"(?i)\b(?:ek\s+)?(?:short\s+)?(?:video|film|reel|movie)\s+(?:banao|bana\s+do|bana\s+de|banado|create\s+karo)\b"),
    re.compile(r"(?i)\b(?:please\s+)?(?:create|make|generate|produce)\s+(?:me\s+)?(?:a|an|the)?\s*(?:short\s+)?(?:video|film|reel|movie)?\s*(?:about|on|of)?\b"),
    re.compile(r"(?i)\bhinglish\b"),
]


def clean_idea(command: str) -> str:
    """The subject of a request, without the production instructions (length, language, format)."""
    text = command
    for pattern in _DIRECTIVES:
        text = pattern.sub(" ", text)
    text = re.sub(r"\s*,\s*(?:,\s*)+", ", ", text)
    text = re.sub(r"\s+", " ", text).strip(" ,.-:;")
    return text if len(text) >= 3 else command.strip()


def brief_from_command(command: str, entities: dict, detected_language: str | None = None, **overrides: object) -> CreativeBrief:
    """Build a brief from a natural-language request, inferring sensible defaults."""
    lang = detected_language or detect_language(command)
    styles = entities.get("styles") or []
    duration = float(entities.get("duration_seconds") or 30.0)
    quoted = (entities.get("quoted") or [""])[0]
    data = {
        "idea": clean_idea(command), "title": quoted, "language": lang if lang in {"en", "hi", "hinglish"} else "en",
        "duration_seconds": max(5.0, min(600.0, duration)), "aspect_ratio": entities.get("aspect_ratio", "9:16"),
        "style": styles[0] if styles else "cinematic", "tone": entities.get("tone", ""),
    }  # fmt: skip
    data.update(overrides)
    return CreativeBrief.model_validate(data)
