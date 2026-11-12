from __future__ import annotations

from pathlib import Path

import pytest

from superagent.core.orchestrator import Platform, format_result
from superagent.creative.project import ProjectStore
from superagent.media.probe import probe
from superagent.types import ExecutionStatus
from tests.conftest import requires_espeak, requires_ffmpeg
from tests.fakes import PaintedImages

pytestmark = [requires_ffmpeg, requires_espeak]


@pytest.fixture
async def studio(platform: Platform, monkeypatch):
    platform.settings.creative_quality = "draft"
    painter = PaintedImages()
    platform.router.register(painter)
    return platform, painter


async def test_idea_to_exported_video_runs_the_whole_pipeline_in_parallel(studio):
    platform, painter = studio
    result = await platform.orchestrator.run('Create a 14-second funny miniature paper pop-up-book video in Hinglish "Chai Wala Raja"')
    # Everything is produced; only the export waits for a human.
    assert result.status is ExecutionStatus.WAITING_APPROVAL, format_result(result)
    by_action = {}
    for t in result.tasks:
        by_action.setdefault(f"{t['agent']}.{t['action']}", []).append(t["status"])
    assert by_action["imager.scene"] == ["SUCCESS"] * 3 and by_action["videographer.scene"] == ["SUCCESS"] * 3
    assert by_action["voice.scene"] == ["SUCCESS"] * 3
    for step in ("subtitles.generate", "editor.render", "thumbnail.create", "content.social", "quality.final"):
        assert by_action[step] == ["SUCCESS"], (step, by_action)
    assert by_action["editor.export"] == ["WAITING_APPROVAL"] and by_action["master.synthesize"] == ["PENDING"]
    assert len(painter.requests) == 3, "one image take per scene at draft quality"

    store = ProjectStore(platform.settings, platform.repo)
    pid = (await platform.repo.list_projects())[0]["id"]
    manifest = store.load(pid)
    assert manifest.brief.language == "hinglish" and manifest.brief.aspect_ratio == "9:16" and manifest.brief.style == "paper pop-up book"
    assert manifest.story.title == "Chai Wala Raja" and manifest.story.method == "template" and len(manifest.story.scenes) == 3
    render = manifest.outputs["render"]
    info = await probe(Path(render["path"]))
    assert (info.video.width, info.video.height) == (540, 960) and info.video.codec == "h264" and info.audio.codec == "aac"
    assert abs(info.duration - render["expected_duration"]) < 1.0 and info.duration > 8
    assert manifest.outputs["qc"]["final_ready"] is True and "exports" not in str(manifest.outputs.get("export", ""))
    for key in ("youtube", "cover", "instagram"):
        assert Path(manifest.outputs["thumbnail"][key]).is_file()
    captions = (platform.settings.projects_dir / pid / "subtitles" / "captions.srt").read_text(encoding="utf-8")
    assert "Milte hain" in captions and captions.count("-->") >= 3

    generations = await platform.repo.list_generations(pid)
    assert {g["kind"] for g in generations} == {"image", "video"} and all(g["selected"] for g in generations)
    assert all(g["qc"]["ok"] for g in generations) and all(g["cache_key"] for g in generations)

    # approve → resume → exported; completed scenes are not regenerated
    approval = result.approvals[0]
    assert approval["action"] == "creative.export"
    await platform.approvals.decide(approval["id"], True, decided_by="pradeep")
    before = len(painter.requests)
    done = await platform.orchestrator.resume(result.execution_id)
    assert done.status is ExecutionStatus.SUCCESS and done.final_ready, format_result(done)
    assert len(painter.requests) == before, "resume must not regenerate finished scenes"
    export_dir = platform.settings.projects_dir / pid / "exports"
    assert (export_dir / "package.json").is_file() and any(p.suffix == ".mp4" for p in export_dir.iterdir())
    assert "nothing was uploaded" in (export_dir / "package.json").read_text()
    assert any("camera motion" in w for w in done.warnings), "the user is told the clips are camera moves, not generated video"


async def test_identical_generation_is_served_from_cache(studio):
    platform, painter = studio
    from superagent.creative.agents import svc  # noqa: F401
    from superagent.creative.models import CreativeBrief
    from superagent.creative.story import StoryEngine, resolve_style

    store = ProjectStore(platform.settings, platform.repo)
    brief = CreativeBrief(idea="a fox learns to fly", duration_seconds=10, quality="draft", language="en")
    manifest = await store.create(brief)
    _, style = resolve_style("clay animation")
    manifest.style = style
    manifest.story = StoryEngine(None).template(brief, style)
    store.save_manifest(manifest)
    scene = manifest.story.scenes[0]
    engine = platform.creative_services.generation
    first = await engine.scene_image(platform.ai, manifest, scene)
    again = await engine.scene_image(platform.ai, manifest, scene)
    forced = await engine.scene_image(platform.ai, manifest, scene, force=True)
    assert not first["cached"] and again["cached"] and not forced["cached"]
    assert len(painter.requests) == 2 and again["path"] == first["path"]


async def test_best_take_is_chosen_by_score_and_blank_images_are_rejected(studio, monkeypatch):
    platform, painter = studio
    from PIL import Image

    from superagent.creative.models import CreativeBrief
    from superagent.creative.story import StoryEngine, resolve_style
    from superagent.errors import MediaError

    store = ProjectStore(platform.settings, platform.repo)
    brief = CreativeBrief(idea="a lighthouse at dusk", duration_seconds=8, quality="standard", language="en")
    manifest = await store.create(brief)
    _, style = resolve_style("cinematic")
    manifest.story = StoryEngine(None).template(brief, style)
    store.save_manifest(manifest)
    result = await platform.creative_services.generation.scene_image(platform.ai, manifest, manifest.story.scenes[0])
    assert len(result["candidates"]) == 2 and result["score"] == max(c["score"] for c in result["candidates"])

    original = painter.generate

    async def blank(request, out_dir):
        results = await original(request, out_dir)
        for r in results:
            Image.new("RGB", (request.width, request.height), (10, 10, 10)).save(r.path)
        return results

    monkeypatch.setattr(painter, "generate", blank)
    with pytest.raises(MediaError, match="blank or a flat colour"):
        await platform.creative_services.generation.scene_image(platform.ai, manifest, manifest.story.scenes[1])


async def test_image_only_and_voice_only_requests(studio):
    platform, _ = studio
    result = await platform.orchestrator.run("make a poster image of a lighthouse at dusk 16:9")
    assert result.status is ExecutionStatus.SUCCESS and result.artifacts and result.artifacts[0].endswith(".png")
    spoken = await platform.orchestrator.run('voiceover "नमस्ते दोस्तों, स्वागत है"')
    assert spoken.status is ExecutionStatus.SUCCESS and spoken.artifacts[0].endswith(".wav")
