from __future__ import annotations

import json

import httpx

from superagent.config import Settings
from superagent.providers.base import ChatMessage as Message
from superagent.providers.base import LLMRequest
from superagent.providers.registry import FREE_PRESETS, build_router
from superagent.providers.vendors.openai_compat import OpenAICompatLLM, make_endpoint, model_picker
from superagent.types import ProviderStatus, ProviderTier

PICKERS = {name: picker for name, _url, _key, _model, picker in FREE_PRESETS}

# OpenRouter's free list as served on 2026-11-13.
OPENROUTER_FREE = [
    "baidu/ernie-5.5-flash-preview:free", "thinkingmachines/inkling-2-small:free", "liquid/lfm-3-1.7b:free",
    "particle-ai/particle-1:free", "recursive/flint-v1.0:free", "nvidia/nemotron-3.5-super-vl:free",
    "dots-studio/dots-3-note-preview:free", "nvidia/nemotron-3.5-lightning:free", "cohere/north-mini-code:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free", "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-26b-a4b-it:free", "google/gemma-4-31b-it:free",
]  # fmt: skip


def test_openrouter_picks_a_strong_free_model():
    assert PICKERS["openrouter"](["openai/gpt-6-astra", *OPENROUTER_FREE]) == "google/gemma-4-31b-it:free"


def test_openrouter_never_picks_a_paid_model():
    assert PICKERS["openrouter"](["openai/gpt-6-astra", "anthropic/claude-opus-5-5"]) is None


def test_gemini_picks_the_newest_stable_flash():
    ids = ["gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-3.8-flash-preview", "gemini-4-argon",
           "gemini-3.6-flash-image", "text-embedding-005"]  # fmt: skip
    assert PICKERS["gemini"](ids) == "gemini-3.6-flash"


def test_groq_skips_speech_and_guard_models():
    assert PICKERS["groq"](["whisper-large-v3", "llama-guard-5", "openai/gpt-oss-120b", "llama-3.1-8b-instant"]) == "openai/gpt-oss-120b"


def test_picker_returns_none_on_an_empty_pool():
    assert model_picker(include=("x",))(["a", "b"]) is None


def _llm(handler) -> OpenAICompatLLM:  # noqa: ANN001
    endpoint = make_endpoint(label="t", base_url="https://free.example/v1", api_key="k", requires_key=True, key_hint="set T_KEY")
    endpoint._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAICompatLLM(endpoint, name="t", model=None, tier=ProviderTier.FREE, model_hint="T_MODEL",
                           pick_model=model_picker(include=(":free",)))  # fmt: skip


async def test_model_is_discovered_once_then_used():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "paid/big"}, {"id": "models/open/small:free"}]})
        body = json.loads(request.content)
        assert body["model"] == "open/small:free"
        return httpx.Response(200, json={"choices": [{"message": {"content": "namaste"}, "finish_reason": "stop"}], "usage": {}})

    llm = _llm(handler)
    assert llm.readiness().status is ProviderStatus.AVAILABLE
    for _ in range(2):
        reply = await llm.complete(LLMRequest(messages=[Message(role="user", content="hi")], max_tokens=20))
        assert reply.text == "namaste"
    assert calls.count("/v1/models") == 1


async def test_no_suitable_model_is_a_clear_configuration_error():
    llm = _llm(lambda request: httpx.Response(200, json={"data": [{"id": "paid/big"}]}))
    try:
        await llm.complete(LLMRequest(messages=[Message(role="user", content="hi")], max_tokens=20))
    except Exception as exc:  # noqa: BLE001
        assert "T_MODEL" in str(exc) and getattr(exc, "status", None) is ProviderStatus.NOT_CONFIGURED
    else:
        raise AssertionError("expected a configuration error")


async def test_presets_are_free_tier_and_need_their_key(settings: Settings, monkeypatch):
    router = await build_router(settings)
    health = {h.name: h for h in router.health()}
    for name in ("groq", "gemini", "openrouter"):
        assert health[name].status is ProviderStatus.NOT_CONFIGURED and "API_KEY" in health[name].detail
    monkeypatch.setenv("GROQ_API_KEY", "proxy-injected")
    router = await build_router(Settings())
    groq = {h.name: h for h in router.health()}["groq"]
    assert groq.status is ProviderStatus.AVAILABLE and groq.tier is ProviderTier.FREE
