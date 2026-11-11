from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from PIL import Image
from pydantic import BaseModel

from superagent.config import Settings
from superagent.core.approval import ApprovalManager
from superagent.core.cost import CostEngine
from superagent.db.repo import Repo
from superagent.errors import ApprovalRequired, NoProviderAvailable, ProviderError, ValidationFailed
from superagent.media.probe import probe, probe_image
from superagent.providers.base import (
    ChatMessage,
    ImageRequest,
    LLMRequest,
    MotionPlan,
    VideoRequest,
    VoiceRequest,
)
from superagent.providers.registry import build_router
from superagent.providers.router import ProviderRouter
from superagent.providers.services import AI
from superagent.providers.templating import fill_template, nearest_aspect
from superagent.providers.vendors.anthropic_sdk import AnthropicLLM
from superagent.providers.vendors.comfyui import ComfyUIImage
from superagent.providers.vendors.fal import FalImage, FalVideo
from superagent.providers.vendors.ffmpeg_motion import FFmpegMotionVideo, resolve_move
from superagent.providers.vendors.hashing import HashingEmbedding, cosine
from superagent.providers.vendors.local_voice import EspeakVoice
from superagent.providers.vendors.ollama import OllamaLLM
from superagent.providers.vendors.openai_compat import (
    OpenAICompatEmbedding,
    OpenAICompatImage,
    OpenAICompatLLM,
    OpenAICompatVoice,
    make_endpoint,
)
from superagent.providers.vendors.replicate import ReplicateImage, ReplicateVideo
from superagent.types import ProviderKind, ProviderStatus, ProviderTier
from tests.conftest import requires_espeak, requires_ffmpeg
from tests.fakes import FakeVendors, PaintedImages, ScriptedLLM, png_bytes


def ask(text: str = "ping", **kw) -> LLMRequest:
    return LLMRequest(messages=[ChatMessage(role="user", content=text)], **kw)


def openai_llm(vendors: FakeVendors, *, key: str | None = "k", name: str = "gw") -> OpenAICompatLLM:
    endpoint = make_endpoint(label=name, base_url=f"{vendors.base_url}/v1", api_key=key, requires_key=False, key_hint="x")
    return OpenAICompatLLM(endpoint, name=name, model="fake-model", tier=ProviderTier.FREE, model_hint="X_MODEL")


# ------------------------------------------------------------------- router
async def test_app_boots_with_no_credentials_and_reports_honest_status(settings: Settings):
    router = await build_router(settings)
    health = {h.name: h for h in router.health()}
    for name in ("ollama", "gateway", "anthropic", "openai", "bedrock", "replicate-image", "fal-video", "comfyui-image"):
        assert health[name].status is ProviderStatus.NOT_CONFIGURED, name
        assert health[name].detail  # says what to set
    assert health["hashing"].status is ProviderStatus.AVAILABLE
    with pytest.raises(NoProviderAvailable) as error:
        await AI(router).complete("hello")
    assert "anthropic" in error.value.reasons and "OmniRoute" in (error.value.hint or "")
    await router.aclose()


async def test_router_prefers_local_then_free_then_cloud():
    router = ProviderRouter()
    cloud, free, local = ScriptedLLM("cloud", "c"), ScriptedLLM("free", "f"), ScriptedLLM("local", "l")
    cloud.tier, free.tier, local.tier = ProviderTier.CLOUD_PRIMARY, ProviderTier.FREE, ProviderTier.LOCAL
    for provider in (cloud, free, local):  # registered in the "wrong" order on purpose
        router.register(provider)
    assert [p.name for p in router.providers(ProviderKind.LLM)] == ["local", "free", "cloud"]
    response, used = await router.run(ProviderKind.LLM, lambda p: p.complete(ask()))
    assert (response.text, used.name) == ("l", "local")


async def test_router_falls_back_cools_down_and_recovers():
    now = [1000.0]
    router = ProviderRouter(clock=lambda: now[0])

    def limited(_request: LLMRequest) -> str:
        raise ProviderError("primary", ProviderStatus.RATE_LIMITED, "429", retry_after=30)

    primary, backup = ScriptedLLM("primary", limited), ScriptedLLM("backup", "from-backup")
    backup.tier = ProviderTier.FREE
    router.register(primary)
    router.register(backup)

    response, used = await router.run(ProviderKind.LLM, lambda p: p.complete(ask()))
    assert used.name == "backup" and len(primary.requests) == 1
    await router.run(ProviderKind.LLM, lambda p: p.complete(ask()))
    assert len(primary.requests) == 1, "a rate-limited provider must not be hit again while cooling down"
    state = {h.name: h for h in router.health()}["primary"]
    assert state.status is ProviderStatus.RATE_LIMITED and 0 < state.cooldown_seconds <= 30

    now[0] += 31  # the provider asked for 30s; after that it is tried again
    primary.reply = "recovered"
    response, used = await router.run(ProviderKind.LLM, lambda p: p.complete(ask()))
    assert (response.text, used.name) == ("recovered", "primary")


async def test_request_rejections_do_not_penalise_and_exhaustion_explains_itself():
    router = ProviderRouter()

    def rejects(_request: LLMRequest) -> str:
        raise ProviderError("picky", ProviderStatus.UNAVAILABLE, "prompt too long", penalize=False)

    router.register(ScriptedLLM("picky", rejects))
    with pytest.raises(NoProviderAvailable) as error:
        await router.run(ProviderKind.LLM, lambda p: p.complete(ask()))
    assert "prompt too long" in error.value.reasons["picky"]
    assert router.health()[0].status is ProviderStatus.AVAILABLE  # still healthy, no cooldown
    with pytest.raises(NoProviderAvailable) as excluded:
        await router.run(ProviderKind.LLM, lambda p: p.complete(ask()), exclude=["picky"])
    assert "excluded" in excluded.value.reasons["picky"]


async def test_router_limits_concurrency_per_provider():
    router = ProviderRouter(concurrency_per_provider=2)
    active = peak = 0

    class Slow(ScriptedLLM):
        async def complete(self, request: LLMRequest):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.03)
            active -= 1
            return await super().complete(request)

    router.register(Slow("slow"))
    await asyncio.gather(*(router.run(ProviderKind.LLM, lambda p: p.complete(ask())) for _ in range(8)))
    assert peak == 2


# -------------------------------------------------------- OpenAI-compatible
async def test_openai_compatible_chat_over_the_wire(vendors: FakeVendors):
    llm = openai_llm(vendors)
    response = await llm.complete(ask("hello", system="be brief", json_mode=True))
    assert response.text == "pong" and response.usage.input_tokens == 11 and response.usage.output_tokens == 7
    sent = vendors.requests("chat")[0]
    assert sent["headers"]["authorization"] == "Bearer k"
    assert sent["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert "temperature" not in sent["body"]
    await llm.aclose()


@pytest.mark.parametrize(
    ("status", "headers", "expected", "retry_after"),
    [(401, {}, ProviderStatus.AUTH_ERROR, None), (429, {"retry-after": "17"}, ProviderStatus.RATE_LIMITED, 17.0),
     (503, {}, ProviderStatus.TEMPORARY_ERROR, None), (404, {}, ProviderStatus.UNAVAILABLE, None)],
)  # fmt: skip
async def test_http_errors_map_to_provider_status(vendors: FakeVendors, status, headers, expected, retry_after):
    vendors.script("chat", status, headers=headers)
    llm = openai_llm(vendors)
    with pytest.raises(ProviderError) as error:
        await llm.complete(ask())
    assert error.value.status is expected and error.value.retry_after == retry_after
    await llm.aclose()


async def test_unreachable_endpoint_is_a_network_error():
    endpoint = make_endpoint(label="dead", base_url="http://127.0.0.1:9/v1", api_key=None, requires_key=False, key_hint="x")
    llm = OpenAICompatLLM(endpoint, name="dead", model="m", tier=ProviderTier.FREE, model_hint="X")
    with pytest.raises(ProviderError) as error:
        await llm.complete(ask())
    assert error.value.status is ProviderStatus.NETWORK_ERROR
    await llm.aclose()


async def test_adapter_adjusts_to_server_dialect_once(vendors: FakeVendors):
    vendors.script("chat", 400, {"error": {"message": "Unknown parameter: response_format"}})
    vendors.script("chat", 400, {"error": {"message": "Unsupported parameter 'max_tokens'. Use 'max_completion_tokens'."}})
    llm = openai_llm(vendors)
    assert (await llm.complete(ask(json_mode=True))).text == "pong"
    final = vendors.requests("chat")[-1]["body"]
    assert "response_format" not in final and final["max_completion_tokens"] == 4096 and "max_tokens" not in final
    await llm.aclose()


async def test_complete_json_validates_and_repairs(vendors: FakeVendors, repo: Repo, settings: Settings):
    class Plan(BaseModel):
        title: str
        steps: list[str]

    replies = iter(['Sure! {"title": "Trip"}', '```json\n{"title": "Trip", "steps": ["book", "pack"]}\n```'])
    vendors.chat = lambda body: next(replies)
    router = ProviderRouter()
    router.register(openai_llm(vendors))
    ai = AI(router, CostEngine(repo, settings))
    plan = await ai.complete_json("Plan a trip", Plan)
    assert plan.steps == ["book", "pack"]
    second_prompt = vendors.requests("chat")[1]["body"]["messages"][-1]["content"]
    assert "previous reply was invalid" in second_prompt and "steps" in second_prompt

    vendors.chat = lambda body: "I cannot produce JSON"
    with pytest.raises(ValidationFailed):
        await ai.complete_json("Plan a trip", Plan)
    assert await repo.cost_sum() == 0.0  # free tier: recorded, costs nothing
    assert len(await repo.list_costs()) == 4
    await router.aclose()


async def test_openai_compatible_image_speech_and_embeddings(vendors: FakeVendors, tmp_path: Path):
    endpoint = make_endpoint(label="gw", base_url=f"{vendors.base_url}/v1", api_key=None, requires_key=False, key_hint="x")
    images = OpenAICompatImage(endpoint, name="gw-image", model="img", tier=ProviderTier.FREE, model_hint="X")
    results = await images.generate(ImageRequest(prompt="a red kite", negative_prompt="blur", width=1080, height=1920, num_images=2), tmp_path)
    assert len(results) == 2
    info = probe_image(results[0].path)
    assert (info.width, info.height) == (1080, 1920), "output is fitted to the exact requested size"
    sent = vendors.requests("images")[0]["body"]
    assert sent["size"] == "1024x1536" and "Avoid: blur" in sent["prompt"]

    voice = OpenAICompatVoice(endpoint, name="gw-voice", model="tts", voice="alloy", tier=ProviderTier.FREE, model_hint="X_MODEL")
    spoken = await voice.text_to_speech(VoiceRequest(text="namaste"), tmp_path / "line.wav")
    assert spoken.path.stat().st_size > 1000 and vendors.requests("speech")[0]["body"]["voice"] == "alloy"

    embedder = OpenAICompatEmbedding(endpoint, name="gw-embed", model="e", tier=ProviderTier.FREE, model_hint="X")
    vectors = await embedder.embed(["a", "four"])
    assert [v[0] for v in vectors] == [1.0, 4.0], "rows are re-ordered by index"
    await endpoint.aclose()


# ------------------------------------------------------------------- Ollama
async def test_ollama_discovers_a_chat_model_and_completes(vendors: FakeVendors):
    llm = OllamaLLM(vendors.base_url, None)
    response = await llm.complete(ask("hi", json_mode=True))
    assert response.text == "pong" and llm.model == "llama-test:latest"  # skipped the embedding model
    body = vendors.requests("ollama-chat")[0]["body"]
    assert body["stream"] is False and body["format"] == "json" and body["options"]["num_predict"] == 4096
    assert OllamaLLM(None, None).readiness().status is ProviderStatus.NOT_CONFIGURED
    await llm.aclose()


# ---------------------------------------------------------------- Anthropic
async def test_anthropic_adapter_uses_the_sdk_with_current_request_shape(vendors: FakeVendors, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", vendors.base_url)
    llm = AnthropicLLM("test-key", "claude-opus-5-5", effort="medium")
    response = await llm.complete(ask("hello", system="You are terse.", max_tokens=1000))
    assert response.text == "pong" and response.usage.input_tokens == 12 and response.provider == "anthropic"
    sent = vendors.requests("anthropic")[0]
    body = sent["body"]
    assert body["model"] == "claude-opus-5-5" and body["system"] == "You are terse."
    assert body["output_config"] == {"effort": "medium"}
    assert body["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in sent["headers"]["anthropic-beta"]
    for removed in ("temperature", "top_p", "top_k", "thinking"):
        assert removed not in body
    # How the key travels (x-api-key vs Authorization) is the SDK's business; it must simply be sent.
    assert "test-key" in (sent["headers"].get("x-api-key", "") + sent["headers"].get("authorization", ""))
    assert sent["headers"]["anthropic-version"]
    await llm.aclose()


async def test_anthropic_refusal_and_errors_are_translated(vendors: FakeVendors, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_BASE_URL", vendors.base_url)
    llm = AnthropicLLM("test-key", "claude-haiku-5-5", refusal_fallback=True)
    vendors.script("anthropic", 200, {
        "id": "msg_r", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
        "stop_reason": "refusal", "stop_sequence": None, "stop_details": {"type": "refusal", "category": "cyber", "explanation": "x"},
        "usage": {"input_tokens": 5, "output_tokens": 0},
    })  # fmt: skip
    with pytest.raises(ProviderError) as refused:
        await llm.complete(ask())
    assert "declined" in refused.value.message and refused.value.penalize is False
    assert "fallbacks" not in vendors.requests("anthropic")[0]["body"], "haiku has no server-side fallback"

    vendors.script("anthropic", 401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    with pytest.raises(ProviderError) as auth:
        await llm.complete(ask())
    assert auth.value.status is ProviderStatus.AUTH_ERROR

    for _ in range(2):  # the SDK retries a 429 once before surfacing it
        vendors.script("anthropic", 429, {"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}, {"retry-after": "1"})
    with pytest.raises(ProviderError) as limited:
        await llm.complete(ask())
    assert limited.value.status is ProviderStatus.RATE_LIMITED and limited.value.retry_after == 1.0
    assert AnthropicLLM(None, "claude-opus-5-5").readiness().status is ProviderStatus.NOT_CONFIGURED
    await llm.aclose()


# ------------------------------------------------------ Replicate / fal / Comfy
async def test_replicate_image_polls_downloads_and_fits(vendors: FakeVendors, tmp_path: Path):
    provider = ReplicateImage("r8_token", "black-forest-labs/flux-schnell", {"go_fast": True, "megapixels": "{{aspect_ratio}}"}, f"{vendors.base_url}/v1")
    results = await provider.generate(ImageRequest(prompt="paper diorama", width=1080, height=1920, seed=42), tmp_path)
    sent = vendors.requests("replicate")[0]
    assert sent["headers"]["authorization"] == "Bearer r8_token" and sent["headers"]["prefer"] == "wait"
    assert sent["body"]["input"] == {
        "prompt": "paper diorama", "aspect_ratio": "9:16", "num_outputs": 1, "output_format": "png", "seed": 42,
        "go_fast": True, "megapixels": "9:16",
    }  # fmt: skip
    assert vendors.polls["p-image"] == 2, "kept polling until the prediction succeeded"
    assert (probe_image(results[0].path).width, probe_image(results[0].path).height) == (1080, 1920)
    assert ReplicateImage(None, "x").readiness().status is ProviderStatus.NOT_CONFIGURED
    await provider.aclose()


@requires_ffmpeg
async def test_replicate_and_fal_video_send_the_image_and_save_a_real_clip(vendors: FakeVendors, tmp_path: Path):
    still = tmp_path / "still.png"
    still.write_bytes(png_bytes(160, 284))
    request = VideoRequest(prompt="camera drifts", image=still, duration=4, width=576, height=1024)

    replicate = ReplicateVideo("tok", "acme/video-model", "first_frame", {"duration": "{{duration_int}}"}, f"{vendors.base_url}/v1")
    clip = await replicate.image_to_video(request, tmp_path / "r.mp4")
    payload = vendors.requests("replicate")[0]["body"]["input"]
    assert payload["first_frame"].startswith("data:image/png;base64,") and payload["duration"] == 4
    assert (await probe(clip.path)).has_video

    fal = FalVideo("fal-key", "acme/video", "image_url", {"aspect_ratio": "{{aspect_ratio}}"}, f"{vendors.base_url}/fal")
    clip = await fal.image_to_video(request, tmp_path / "f.mp4")
    sent = vendors.requests("fal")[0]
    assert sent["headers"]["authorization"] == "Key fal-key" and sent["body"]["aspect_ratio"] == "9:16"
    assert (await probe(clip.path)).has_video
    assert ReplicateVideo("tok", None).readiness().status is ProviderStatus.NOT_CONFIGURED
    await replicate.aclose()
    await fal.aclose()


async def test_fal_image_queue_flow(vendors: FakeVendors, tmp_path: Path):
    provider = FalImage("fal-key", "fal-ai/flux/schnell", None, f"{vendors.base_url}/fal")
    results = await provider.generate(ImageRequest(prompt="clay fox", width=512, height=512), tmp_path)
    assert vendors.requests("fal")[0]["body"]["image_size"] == {"width": 512, "height": 512}
    assert results[0].seed == 77 and probe_image(results[0].path).width == 512
    await provider.aclose()


async def test_comfyui_fills_the_workflow_uploads_and_collects_outputs(vendors: FakeVendors, tmp_path: Path):
    workflow = tmp_path / "wf.json"
    workflow.write_text(json.dumps({
        "3": {"class_type": "KSampler", "inputs": {"seed": "{{seed}}", "steps": 4}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": "{{width}}", "height": "{{height}}"}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}, best quality"}},
        "7": {"class_type": "LoadImage", "inputs": {"image": "{{image}}"}},
    }))  # fmt: skip
    reference = tmp_path / "ref.png"
    reference.write_bytes(png_bytes())
    provider = ComfyUIImage(vendors.base_url, workflow)
    assert provider.readiness().ok
    results = await provider.generate(
        ImageRequest(prompt="paper fox", width=768, height=1344, seed=9, reference_images=[reference]), tmp_path / "out"
    )
    graph = vendors.requests("comfy-prompt")[0]["body"]["prompt"]
    assert graph["3"]["inputs"]["seed"] == 9 and graph["5"]["inputs"] == {"width": 768, "height": 1344}
    assert graph["6"]["inputs"]["text"] == "paper fox, best quality" and graph["7"]["inputs"]["image"] == "uploaded.png"
    assert vendors.requests("comfy-upload") and probe_image(results[0].path).height == 1344

    broken = tmp_path / "broken.json"
    broken.write_text('{"nodes": []}')
    assert "API-format" in ComfyUIImage(vendors.base_url, broken).readiness().reason
    assert ComfyUIImage(None, workflow).readiness().status is ProviderStatus.NOT_CONFIGURED
    await provider.aclose()


# --------------------------------------------------------- local media engines
@requires_ffmpeg
@pytest.mark.parametrize("camera", ["push-in", "pan-left", "handheld", "orbit"])
async def test_ffmpeg_motion_renders_a_valid_clip_and_says_what_it_is(tmp_path: Path, camera: str):
    still = tmp_path / "still.png"
    Image.new("RGB", (640, 360), (30, 90, 160)).save(still)
    provider = FFmpegMotionVideo()
    request = VideoRequest(image=still, duration=1.5, fps=24, width=270, height=480, motion=MotionPlan(camera=camera))
    result = await provider.image_to_video(request, tmp_path / f"{camera}.mp4")
    info = await probe(result.path)
    assert info.video and (info.video.width, info.video.height) == (270, 480)
    assert abs(info.duration - 1.5) < 0.1 and abs(info.video.fps - 24) < 0.5 and not info.has_audio
    assert result.generative is False and "camera motion only" in result.metadata["notes"][0]
    assert result.metadata["approximated"] is (camera == "orbit")
    with pytest.raises(ProviderError):
        await provider.text_to_video(request, tmp_path / "x.mp4")


def test_camera_aliases():
    assert resolve_move("Pull_Out") == ("pull-out", False)
    assert resolve_move("dolly") == ("push-in", True)
    assert resolve_move("something new") == ("push-in", True)


@requires_espeak
@requires_ffmpeg
async def test_espeak_speaks_hindi_and_english_offline(tmp_path: Path):
    voice = EspeakVoice()
    hindi = await voice.text_to_speech(VoiceRequest(text="नमस्ते, यह एक परीक्षण है।", language="hi"), tmp_path / "hi.wav")
    english = await voice.text_to_speech(VoiceRequest(text="--help is just text here", speed=1.3), tmp_path / "en.mp3")
    assert hindi.voice == "hi" and english.voice == "en-us"
    for result in (hindi, english):
        info = await probe(result.path)
        assert info.has_audio and info.duration > 0.5
    assert any(v.id == "hi" for v in await voice.list_voices())
    assert EspeakVoice("no-such-espeak").readiness().status is ProviderStatus.NOT_CONFIGURED


async def test_hashing_embeddings_rank_related_text_higher():
    embedder = HashingEmbedding()
    query, related, unrelated, hindi_q, hindi_d = await embedder.embed([
        "quarterly sales report for the north region", "north region sales figures this quarter",
        "how to bake sourdough bread at home", "पेंशन आवेदन की स्थिति", "पेंशन आवेदन स्थिति रिपोर्ट",
    ])  # fmt: skip
    assert len(query) == 384
    assert cosine(query, related) > cosine(query, unrelated) + 0.2
    assert cosine(hindi_q, hindi_d) > cosine(hindi_q, unrelated) + 0.2


def test_template_substitution_keeps_types():
    filled = fill_template({"seed": "{{seed}}", "text": "{{prompt}} in 4k", "nested": [{"w": "{{ width }}"}], "n": 3},
                           {"seed": 7, "prompt": "fox", "width": 512})  # fmt: skip
    assert filled == {"seed": 7, "text": "fox in 4k", "nested": [{"w": 512}], "n": 3}
    assert nearest_aspect(1080, 1920) == "9:16" and nearest_aspect(1920, 1080) == "16:9" and nearest_aspect(1000, 1000) == "1:1"


# --------------------------------------------------------------------- cost
async def test_free_and_local_calls_are_never_gated(repo: Repo, settings: Settings):
    cost = CostEngine(repo, settings, ApprovalManager(repo, settings))
    estimate = await cost.authorize(provider="ollama", model="x", kind="llm", units={"input_tokens": 10**9}, tier=ProviderTier.LOCAL)
    assert estimate.usd == 0.0


async def test_paid_call_over_budget_needs_approval_then_is_recorded(repo: Repo, monkeypatch):
    monkeypatch.setenv("SAX_PER_GENERATION_BUDGET_USD", "0.01")
    settings = Settings()
    approvals = ApprovalManager(repo, settings)
    cost = CostEngine(repo, settings, approvals)
    args = dict(provider="replicate-video", model="acme/video", kind="video", units={"seconds": 5},
                tier=ProviderTier.CLOUD_PRIMARY, execution_id="exe_1", task_id="t1")  # fmt: skip
    with pytest.raises(ApprovalRequired) as pending:
        await cost.authorize(**args)
    assert "per-generation budget" in pending.value.reason
    await approvals.decide(pending.value.approval_id, True)
    estimate = await cost.authorize(**args)
    assert estimate.usd == settings.unpriced_estimate_usd and estimate.priced is False
    await cost.record(provider="replicate-video", model="acme/video", kind="video", units={"seconds": 5},
                      tier=ProviderTier.CLOUD_PRIMARY, estimate=estimate, execution_id="exe_1")  # fmt: skip
    summary = await cost.summary()
    assert summary["by_provider"]["replicate-video"] == {"calls": 1, "usd": 0.05, "unpriced_calls": 1}


async def test_priced_models_use_the_price_table_and_daily_budget(repo: Repo, monkeypatch, tmp_path: Path):
    prices = tmp_path / "prices.json"
    prices.write_text(json.dumps({"fal-image": {"*": {"per_image": 0.4}}}))
    monkeypatch.setenv("SAX_PRICES_FILE", str(prices))
    monkeypatch.setenv("SAX_DAILY_BUDGET_USD", "1.0")
    monkeypatch.setenv("SAX_PROJECT_BUDGET_USD", "50")
    settings = Settings()
    cost = CostEngine(repo, settings, ApprovalManager(repo, settings))
    llm = cost.estimate(provider="anthropic", model="claude-opus-5-5", kind="llm",
                        units={"input_tokens": 1_000_000, "output_tokens": 500_000}, tier=ProviderTier.CLOUD_PRIMARY)  # fmt: skip
    assert llm.priced and llm.usd == pytest.approx(4.0 + 10.0)
    image = dict(provider="fal-image", model="any", kind="image", units={"images": 1}, tier=ProviderTier.CLOUD_PRIMARY)
    for _ in range(2):
        estimate = await cost.authorize(**image)
        await cost.record(**image, estimate=estimate)
    with pytest.raises(ApprovalRequired) as pending:  # third image would take today's spend to $1.20
        await cost.authorize(**image)
    assert "daily budget" in pending.value.reason


async def test_ai_facade_routes_images_and_records_usage(repo: Repo, settings: Settings, tmp_path: Path):
    router = ProviderRouter()
    painter = PaintedImages()
    router.register(painter)
    ai = AI(router, CostEngine(repo, settings), project_id="prj_1")
    results = await ai.image(ImageRequest(prompt="x", width=320, height=180, num_images=3), tmp_path)
    assert len(results) == 3 and all(r.path.is_file() for r in results)
    assert (await repo.list_costs(project_id="prj_1"))[0]["units"] == {"images": 3}
    with pytest.raises(NoProviderAvailable):  # the painter cannot inpaint, and says so
        await ai.image(ImageRequest(prompt="x"), tmp_path, operation="inpaint")
