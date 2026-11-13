from __future__ import annotations

import json

import pytest

from superagent.core.orchestrator import Platform, format_result
from superagent.creative.factory import parse_request, template_idea, wants_factory
from superagent.types import ExecutionStatus
from tests.conftest import requires_espeak, requires_ffmpeg
from tests.fakes import PaintedImages, ScriptedLLM


@pytest.mark.parametrize(
    ("command", "topic", "category"),
    [
        ("content package on monsoon farming, category: education", "monsoon farming", "education"),
        ("daily content: kids, honesty", "honesty", "kids"),
        ("topic: space travel", "space travel", ""),
    ],
)
def test_factory_requests_are_recognised_and_parsed(command, topic, category):
    assert wants_factory(command)
    assert parse_request(command) == (topic, category)


def test_ordinary_video_requests_do_not_go_to_the_factory():
    assert not wants_factory("chai wale ki kahani video banao")


def test_template_idea_uses_the_topic_and_language():
    idea = template_idea("ईमानदारी", "kids", "hi")
    assert "ईमानदारी" in idea.title and "ईमानदारी" in idea.idea


@requires_ffmpeg
@requires_espeak
async def test_topic_to_exported_package_end_to_end(platform: Platform):
    platform.settings.creative_quality = "draft"
    painter = PaintedImages()
    platform.router.register(painter)
    result = await platform.orchestrator.run("content package on honesty, category: kids, 12 second")
    assert result.status is ExecutionStatus.WAITING_APPROVAL, format_result(result)
    status = {f"{t['agent']}.{t['action']}": t["status"] for t in result.tasks}
    assert status["factory.package"] == "SUCCESS"
    for step in ("director.brief", "director.storyboard", "subtitles.generate", "editor.render",
                 "thumbnail.create", "content.social", "quality.final"):  # fmt: skip
        assert status.get(step) == "SUCCESS", (step, status)
    assert status["editor.export"] == "WAITING_APPROVAL", "publishing stays approval-controlled"
    rows = await platform.repo.load_tasks(result.execution_id)
    out = next(t for t in rows if t["agent"] == "factory")["output"]
    assert out["category"] == "kids" and out["method"] == "template" and "honesty" in out["title"].lower()
    rout = next(t for t in rows if t["agent"] == "editor" and t["action"] == "render")["output"]
    assert rout["music"] == "procedural ambient", "the factory adds an original music bed"


async def test_llm_writes_the_idea_when_available(platform: Platform):
    idea = {"title": "Sach ka Sitara", "idea": "A shy kid returns a lost wallet and the whole street lights up.", "hook": "Kya aap lautaate?", "tone": "warm"}
    platform.router.register(ScriptedLLM(reply=json.dumps(idea)))
    command = "daily content: kids, honesty"
    plan = await platform.planner().plan(command, await platform.intents.detect(command))
    assert [t.agent for t in plan.tasks][:1] == ["factory"]
    run = await platform.orchestrator.run(command)
    out = next(t for t in await platform.repo.load_tasks(run.execution_id) if t["agent"] == "factory")["output"]
    assert out["method"] == "llm" and out["title"] == "Sach ka Sitara"
