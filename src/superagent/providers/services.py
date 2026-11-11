"""`AI`: the one object agents use to reach models.

It routes through the provider router (so fallback, cooldowns and tier order apply),
authorises paid calls against the budget, and records usage. Agents never talk to a
vendor adapter directly.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..core.cost import CostEngine, Estimate
from ..errors import ValidationFailed
from ..types import ProviderKind
from ..utils import estimate_tokens, extract_json, truncate_middle
from .base import (
    BaseProvider,
    ChatMessage,
    ImageProvider,
    ImageRequest,
    ImageResult,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    VideoProvider,
    VideoRequest,
    VideoResult,
    VoiceProvider,
    VoiceRequest,
    VoiceResult,
)
from .router import ProviderRouter

M = TypeVar("M", bound=BaseModel)

JSON_RULES = (
    "Respond with a single JSON object and nothing else: no prose, no code fences. "
    "Use double quotes and do not add comments or trailing commas."
)


class AI:
    def __init__(
        self,
        router: ProviderRouter,
        cost: CostEngine | None = None,
        *,
        execution_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
        exclude: Iterable[str] = (),
    ):
        self.router = router
        self.cost = cost
        self.execution_id = execution_id
        self.project_id = project_id
        self.task_id = task_id
        self.exclude = tuple(exclude)

    def scoped(self, **overrides: Any) -> AI:
        params: dict[str, Any] = {
            "execution_id": self.execution_id, "project_id": self.project_id,
            "task_id": self.task_id, "exclude": self.exclude,
        }  # fmt: skip
        params.update(overrides)
        return AI(self.router, self.cost, **params)

    def has(self, kind: ProviderKind, capability: str | None = None) -> bool:
        usable, _ = self.router.candidates(kind, capability=capability, exclude=self.exclude)
        return bool(usable)

    # ---------------------------------------------------------------- plumbing
    def _authorizer(self, kind: str, units: dict[str, Any], estimates: dict[str, Estimate]):  # noqa: ANN202
        async def authorize(provider: BaseProvider) -> None:
            if self.cost is None:
                return
            estimates[provider.name] = await self.cost.authorize(
                provider=provider.name, model=provider.model, kind=kind, units=units, tier=provider.tier,
                execution_id=self.execution_id, project_id=self.project_id, task_id=self.task_id,
            )  # fmt: skip

        return authorize

    async def _record(self, provider: BaseProvider, kind: str, units: dict[str, Any], estimate: Estimate | None) -> None:
        if self.cost is not None:
            await self.cost.record(
                provider=provider.name, model=provider.model, kind=kind, units=units, tier=provider.tier,
                estimate=estimate, execution_id=self.execution_id, project_id=self.project_id,
            )  # fmt: skip

    # --------------------------------------------------------------------- LLM
    async def complete(
        self,
        prompt: str | list[ChatMessage],
        *,
        system: str | None = None,
        max_tokens: int = 4096,
        json_mode: bool = False,
        json_schema: dict[str, Any] | None = None,
        images: list[Path] | None = None,
        only: str | None = None,
    ) -> LLMResponse:
        messages = [ChatMessage(role="user", content=prompt)] if isinstance(prompt, str) else prompt
        request = LLMRequest(
            messages=messages, system=system, max_tokens=max_tokens, json_mode=json_mode,
            json_schema=json_schema, images=images or [],
        )  # fmt: skip
        prompt_tokens = estimate_tokens((system or "") + "".join(m.content for m in messages))
        planned = {"input_tokens": prompt_tokens, "output_tokens": min(max_tokens, 2000)}
        estimates: dict[str, Estimate] = {}

        async def call(provider: LLMProvider) -> LLMResponse:
            return await provider.complete(request)

        response, provider = await self.router.run(
            ProviderKind.LLM, call, capability="vision" if images else None, exclude=self.exclude, only=only,
            authorize=self._authorizer("llm", planned, estimates),
        )  # fmt: skip
        used = {
            "input_tokens": response.usage.input_tokens or prompt_tokens,
            "output_tokens": response.usage.output_tokens or estimate_tokens(response.text),
        }
        await self._record(provider, "llm", used, estimates.get(provider.name))
        return response

    async def complete_json(
        self,
        prompt: str,
        schema: type[M],
        *,
        system: str | None = None,
        max_tokens: int = 8192,
        attempts: int = 2,
        repair_hint: str | None = None,
    ) -> M:
        """Ask for JSON matching ``schema``; on a validation error, show the model its mistake once."""
        schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        base = f"{prompt}\n\n{JSON_RULES}\nThe object must validate against this JSON Schema:\n{schema_text}"
        if repair_hint:
            base += f"\n\nA previous attempt was rejected for this reason; fix it:\n{repair_hint}"
        current, problem = base, ""
        for attempt in range(max(1, attempts)):
            response = await self.complete(current, system=system, max_tokens=max_tokens, json_mode=True)
            try:
                return schema.model_validate(extract_json(response.text))
            except (ValueError, ValidationError) as exc:
                problem = truncate_middle(str(exc), 1500)
                if response.finish_reason == "length":
                    problem = f"the reply was cut off at {max_tokens} tokens; be more concise. {problem}"
                current = (
                    f"{base}\n\nYour previous reply was invalid:\n{problem}\n"
                    f"Previous reply (start):\n{response.text[:1500]}\n\nReturn the corrected JSON object only."
                )
                if attempt + 1 >= attempts:
                    break
        raise ValidationFailed(f"model output did not match {schema.__name__}: {problem}")

    # --------------------------------------------------------------- embedding
    async def embed(self, texts: list[str]) -> tuple[list[list[float]], str]:
        units = {"input_tokens": sum(estimate_tokens(t) for t in texts)}
        estimates: dict[str, Estimate] = {}
        vectors, provider = await self.router.run(
            ProviderKind.EMBEDDING, lambda p: p.embed(texts), exclude=self.exclude,
            authorize=self._authorizer("embedding", units, estimates),
        )  # fmt: skip
        await self._record(provider, "embedding", units, estimates.get(provider.name))
        return vectors, f"{provider.name}:{provider.model}"

    # ------------------------------------------------------------------- media
    async def image(
        self, request: ImageRequest, out_dir: Path, *, operation: str = "generate", only: str | None = None
    ) -> list[ImageResult]:
        units = {"images": request.num_images}
        estimates: dict[str, Estimate] = {}

        async def call(provider: ImageProvider) -> list[ImageResult]:
            return await getattr(provider, operation)(request, out_dir)

        results, provider = await self.router.run(
            ProviderKind.IMAGE, call, capability=operation, exclude=self.exclude, only=only,
            authorize=self._authorizer("image", units, estimates),
        )  # fmt: skip
        await self._record(provider, "image", {"images": len(results)}, estimates.get(provider.name))
        return results

    async def video(
        self, request: VideoRequest, out_path: Path, *, operation: str = "image_to_video", only: str | None = None
    ) -> VideoResult:
        units = {"seconds": request.duration}
        estimates: dict[str, Estimate] = {}

        async def call(provider: VideoProvider) -> VideoResult:
            return await getattr(provider, operation)(request, out_path)

        result, provider = await self.router.run(
            ProviderKind.VIDEO, call, capability=operation, exclude=self.exclude, only=only,
            authorize=self._authorizer("video", units, estimates),
        )  # fmt: skip
        await self._record(provider, "video", units, estimates.get(provider.name))
        return result

    async def speak(self, request: VoiceRequest, out_path: Path, *, only: str | None = None) -> VoiceResult:
        units = {"characters": len(request.text)}
        estimates: dict[str, Estimate] = {}

        async def call(provider: VoiceProvider) -> VoiceResult:
            voice = request.voice or provider.default_voice(request.language)
            return await provider.text_to_speech(request.model_copy(update={"voice": voice}), out_path)

        result, provider = await self.router.run(
            ProviderKind.VOICE, call, capability="text_to_speech", exclude=self.exclude, only=only,
            authorize=self._authorizer("voice", units, estimates),
        )  # fmt: skip
        await self._record(provider, "voice", units, estimates.get(provider.name))
        return result
