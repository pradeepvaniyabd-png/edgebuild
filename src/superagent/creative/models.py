"""Creative data model. ``project.json`` is the serialised ``ProjectManifest``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

Quality = Literal["draft", "standard", "high"]
ASPECTS = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080), "4:5": (1080, 1350)}
ASPECT_FLOAT = {"9:16": 9 / 16, "16:9": 16 / 9, "1:1": 1.0, "4:5": 4 / 5}
# Per-quality knobs: image variations per scene, video takes per scene, and the render size divisor.
QUALITY = {"draft": {"image_takes": 1, "video_takes": 1, "scale": 0.5}, "standard": {"image_takes": 2, "video_takes": 1, "scale": 1.0},
           "high": {"image_takes": 3, "video_takes": 2, "scale": 1.0}}  # fmt: skip


class CreativeBrief(BaseModel):
    idea: str
    title: str = ""
    language: str = "en"  # en | hi | hinglish
    duration_seconds: float = Field(30.0, gt=2, le=600)
    aspect_ratio: str = "9:16"
    style: str = "cinematic"
    tone: str = ""
    audience: str = ""
    quality: Quality = "standard"
    fps: int = Field(30, ge=12, le=60)
    subtitles: bool = True
    music_path: str | None = None
    ambient_music: bool = False  # procedural bed; never copyrighted music
    watermark_path: str | None = None
    notes: list[str] = Field(default_factory=list)

    @field_validator("aspect_ratio")
    @classmethod
    def _aspect(cls, value: str) -> str:
        if value not in ASPECTS:
            raise ValueError(f"aspect_ratio must be one of {', '.join(ASPECTS)}")
        return value

    @property
    def size(self) -> tuple[int, int]:
        width, height = ASPECTS[self.aspect_ratio]
        scale = QUALITY[self.quality]["scale"]
        return int(width * scale) // 2 * 2, int(height * scale) // 2 * 2


class CharacterBible(BaseModel):
    character_id: str
    name: str
    visual_description: str
    body_proportions: str = ""
    hair: str = ""
    clothing: str = ""
    colors: list[str] = Field(default_factory=list)
    accessories: str = ""
    expressions: str = ""
    personality: str = ""
    reference_assets: list[str] = Field(default_factory=list)
    seed: int = 0
    voice: str | None = None

    def prompt_fragment(self) -> str:
        parts = [self.visual_description, self.body_proportions, self.hair, self.clothing, self.accessories]
        palette = f"colors {', '.join(self.colors)}" if self.colors else ""
        return f"{self.name}: " + ", ".join(p.strip() for p in [*parts, palette] if p.strip())


class StyleBible(BaseModel):
    visual_style: str = "cinematic"
    rendering_style: str = ""
    lighting: str = "soft natural light"
    camera_language: str = "smooth, motivated camera moves"
    color_language: str = "cohesive palette"
    materials: str = ""
    environment: str = ""
    depth_of_field: str = "shallow depth of field"
    composition: str = "clear focal subject, rule of thirds"
    aspect_ratio: str = "9:16"
    negative: str = "text, watermark, logo, extra limbs, deformed hands, blurry, low quality"

    def prompt_fragment(self) -> str:
        parts = [self.visual_style, self.rendering_style, self.lighting, self.color_language, self.materials,
                 self.depth_of_field, self.composition]  # fmt: skip
        return ", ".join(p.strip() for p in parts if p.strip())


class Location(BaseModel):
    location_id: str
    name: str
    description: str


class DialogueLine(BaseModel):
    speaker: str
    text: str


class Scene(BaseModel):
    scene_id: str
    duration: float = Field(8.0, gt=0.5, le=60)
    location: str = ""
    time: str = "day"
    characters: list[str] = Field(default_factory=list)
    action: str
    emotion: str = ""
    camera: str = "push-in"
    lens: str = "35mm"
    lighting: str = ""
    environment: str = ""
    dialogue: list[DialogueLine] = Field(default_factory=list)
    narration: str = ""
    music: str = ""
    sfx: str = ""
    image_prompt: str = ""
    video_prompt: str = ""
    negative_prompt: str = ""


class Story(BaseModel):
    title: str
    genre: str = ""
    duration: float
    language: str = "en"
    aspect_ratio: str = "9:16"
    synopsis: str = ""
    characters: list[CharacterBible] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    scenes: list[Scene] = Field(min_length=1, max_length=40)
    method: Literal["llm", "template"] = "llm"


class ProjectManifest(BaseModel):
    project_id: str
    name: str
    brief: CreativeBrief
    status: str = "CREATED"
    story: Story | None = None
    style: StyleBible = Field(default_factory=StyleBible)
    voices: dict[str, str] = Field(default_factory=dict)
    selected: dict[str, dict[str, str]] = Field(default_factory=dict)  # kind -> scene_id -> path
    outputs: dict[str, Any] = Field(default_factory=dict)  # render, subtitles, thumbnail, qc, social, export
    notes: list[str] = Field(default_factory=list)

    def scene(self, scene_id: str) -> Scene:
        if self.story is None:
            raise KeyError("the project has no storyboard yet")
        for scene in self.story.scenes:
            if scene.scene_id == scene_id:
                return scene
        raise KeyError(f"no scene '{scene_id}' in project {self.project_id}")
