"""Creative pipeline: story → images → clips → voice → subtitles → edit → QC → export."""

from __future__ import annotations

from typing import Any

from ..core.dag import Task
from ..core.intent import IntentResult
from ..types import Intent
from .agents import CreativeServices, all_agents
from .factory import ContentFactoryAgent, parse_request, wants_factory

_SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1024, 1024), "4:5": (1080, 1350)}


class CreativePlanner:
    def __init__(self, settings: Any):
        self.settings = settings

    def _task(self, agent: str, action: str, description: str, inputs: dict[str, Any], deps: list[str]) -> Task:
        return Task(description=description, agent=agent, action=action, input=inputs, dependencies=deps,
                    max_retries=self.settings.max_retries, timeout=self.settings.task_timeout)  # fmt: skip

    def wants(self, command: str) -> bool:
        return wants_factory(command)

    def plan(self, command: str, intent: IntentResult, depends_on: list[str], source: str = "") -> list[Task]:
        entities = intent.entities
        if wants_factory(command):
            topic, category = parse_request(command)
            inputs: dict[str, Any] = {"topic": topic or command.strip(), "category": category,
                                      "language": entities.get("language") or (intent.language if intent.language in {"en", "hi", "hinglish"} else None),
                                      "duration_seconds": entities.get("duration_seconds"), "aspect_ratio": entities.get("aspect_ratio") or "9:16"}  # fmt: skip
            if entities.get("styles"):
                inputs["style"] = entities["styles"][0]
            return [self._task("factory", "package", f"content package: {inputs['topic']}", {k: v for k, v in inputs.items() if v not in (None, "")} | {"category": category}, depends_on)]
        if intent.has(Intent.VIDEO, Intent.MULTIMODAL):
            idea = command if not source else f"{command}\n\nSource material:\n{source}"
            brief_inputs: dict[str, Any] = {"idea": idea, "language": entities.get("language") or (intent.language if intent.language in {"en", "hi", "hinglish"} else None),
                                            "duration_seconds": entities.get("duration_seconds"), "aspect_ratio": entities.get("aspect_ratio"),
                                            "tone": entities.get("tone"), "title": (entities.get("quoted") or [""])[0]}  # fmt: skip
            if entities.get("styles"):
                brief_inputs["style"] = entities["styles"][0]
            brief = self._task("director", "brief", "creative brief and project", {k: v for k, v in brief_inputs.items() if v}, depends_on)
            story = self._task("director", "storyboard", "story, bibles, shot list and production plan",
                               {"project_id": f"${{{brief.id}.project_id}}"}, [brief.id])  # fmt: skip
            return [brief, story]
        if intent.has(Intent.IMAGE):
            width, height = _SIZES.get(entities.get("aspect_ratio", ""), (1280, 720) if "thumbnail" in command.lower() else (1024, 1024))
            return [self._task("imager", "create", "generate image(s)", {"prompt": command, "width": width, "height": height}, depends_on)]
        if intent.has(Intent.VOICE):
            text = (entities.get("quoted") or [command])[0]
            return [self._task("voice", "speak", "speak the text", {"text": text}, depends_on)]
        return []


def install(platform: Any) -> None:
    platform.creative_services = CreativeServices(platform)
    for agent in [*all_agents(), ContentFactoryAgent()]:
        if agent.name not in platform.agents:
            platform.agents.register(agent)
    platform.creative = CreativePlanner(platform.settings)
