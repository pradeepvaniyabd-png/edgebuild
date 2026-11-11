"""Provider router: local → free → primary cloud → secondary cloud, with health and fallback.

Rate limits are respected, never bypassed: a limited provider cools down for as
long as it asked and the router moves on to the next candidate.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from ..errors import NoProviderAvailable, ProviderError
from ..observability import get_logger, log_context
from ..types import ProviderKind, ProviderStatus
from ..utils import utcnow
from .base import BaseProvider, ProviderHealth

log = get_logger("providers")

P = TypeVar("P", bound=BaseProvider)
T = TypeVar("T")

_HINTS = {
    ProviderKind.LLM: (
        "run Ollama locally, point SAX_GATEWAY_BASE_URL at an OpenAI-compatible gateway "
        "(OmniRoute, FreeLLMAPI, LM Studio), or set ANTHROPIC_API_KEY / OPENAI_API_KEY"
    ),
    ProviderKind.IMAGE: "set COMFYUI_BASE_URL + COMFYUI_IMAGE_WORKFLOW, REPLICATE_API_TOKEN, FAL_KEY or an image model on the gateway",
    ProviderKind.VIDEO: "configure REPLICATE_VIDEO_MODEL / FAL_VIDEO_MODEL / COMFYUI_VIDEO_WORKFLOW (FFmpeg motion needs ffmpeg)",
    ProviderKind.VOICE: "install espeak-ng or Piper, or configure a TTS model on the gateway / OpenAI",
    ProviderKind.EMBEDDING: "the built-in hashing embedder should always be available",
}


@dataclass
class _State:
    status: ProviderStatus = ProviderStatus.AVAILABLE
    detail: str = ""
    cooldown_until: float = 0.0
    failures: int = 0
    last_success_at: datetime | None = None


class ProviderRouter:
    def __init__(self, *, concurrency_per_provider: int = 2, clock: Callable[[], float] = time.monotonic):
        self._providers: list[BaseProvider] = []
        self._state: dict[str, _State] = {}
        self._limits: dict[str, asyncio.Semaphore] = {}
        self._concurrency = max(1, concurrency_per_provider)
        self._clock = clock

    # ------------------------------------------------------------ registry
    def register(self, provider: BaseProvider) -> None:
        if any(existing.name == provider.name for existing in self._providers):
            raise ValueError(f"provider '{provider.name}' is already registered")
        self._providers.append(provider)
        self._state[provider.name] = _State()

    def providers(self, kind: ProviderKind | None = None) -> list[BaseProvider]:
        chosen = [p for p in self._providers if kind is None or p.kind is kind]
        return sorted(chosen, key=lambda p: (int(p.tier), self._providers.index(p)))

    def get(self, name: str) -> BaseProvider | None:
        return next((p for p in self._providers if p.name == name), None)

    # ------------------------------------------------------------ selection
    def candidates(
        self,
        kind: ProviderKind,
        *,
        capability: str | None = None,
        exclude: Iterable[str] = (),
        only: str | None = None,
    ) -> tuple[list[BaseProvider], dict[str, str]]:
        """Usable providers in routing order, plus the reason each other one was skipped."""
        excluded = set(exclude)
        usable: list[BaseProvider] = []
        skipped: dict[str, str] = {}
        now = self._clock()
        for provider in self.providers(kind):
            if only and provider.name != only:
                continue
            if provider.name in excluded:
                skipped[provider.name] = "excluded after an earlier failure"
                continue
            readiness = provider.readiness()
            if not readiness.ok:
                skipped[provider.name] = f"{readiness.status.value}: {readiness.reason}"
                continue
            if capability and capability not in provider.capabilities:
                skipped[provider.name] = f"does not support '{capability}'"
                continue
            state = self._state[provider.name]
            if state.cooldown_until > now:
                remaining = state.cooldown_until - now
                skipped[provider.name] = f"{state.status.value}: cooling down {remaining:.0f}s ({state.detail})"
                continue
            usable.append(provider)
        return usable, skipped

    def available(self, kind: ProviderKind, capability: str | None = None) -> bool:
        return bool(self.candidates(kind, capability=capability)[0])

    async def run(
        self,
        kind: ProviderKind,
        call: Callable[[Any], Awaitable[T]],
        *,
        capability: str | None = None,
        exclude: Iterable[str] = (),
        only: str | None = None,
        authorize: Callable[[Any], Awaitable[None]] | None = None,
    ) -> tuple[T, BaseProvider]:
        """Try candidates in order. ``ProviderError`` triggers fallback; anything else propagates."""
        usable, reasons = self.candidates(kind, capability=capability, exclude=exclude, only=only)
        for index, provider in enumerate(usable):
            if authorize is not None:
                await authorize(provider)  # may raise ApprovalRequired / BudgetExceeded: no silent fallback
            limit = self._limits.setdefault(provider.name, asyncio.Semaphore(self._concurrency))
            async with limit:
                with log_context(provider=provider.name):
                    try:
                        result = await call(provider)
                    except ProviderError as exc:
                        self._record_failure(provider, exc)
                        reasons[provider.name] = f"{exc.status.value}: {exc.message}"
                        more = "trying next provider" if index + 1 < len(usable) else "no providers left"
                        log.warning("%s failed (%s); %s", provider.name, exc.status.value, more)
                        continue
                    self._record_success(provider)
                    return result, provider
        raise NoProviderAvailable(kind.value, reasons, hint=_HINTS.get(kind))

    # --------------------------------------------------------------- health
    def _record_success(self, provider: BaseProvider) -> None:
        self._state[provider.name] = _State(last_success_at=utcnow())

    def _record_failure(self, provider: BaseProvider, error: ProviderError) -> None:
        if not error.penalize:
            return
        state = self._state[provider.name]
        state.failures += 1
        state.status = error.status
        state.detail = error.message[:200]
        if error.status is ProviderStatus.RATE_LIMITED:
            wait = error.retry_after if error.retry_after is not None else 60.0
        elif error.status is ProviderStatus.AUTH_ERROR:
            wait = 3600.0  # credentials do not fix themselves; `reset` clears it after a config change
        elif error.status is ProviderStatus.UNAVAILABLE:
            wait = 120.0
        else:
            wait = min(300.0, 5.0 * (2 ** min(state.failures - 1, 6)))
            if error.retry_after is not None:
                wait = max(wait, error.retry_after)
        state.cooldown_until = self._clock() + wait

    def reset(self, name: str | None = None) -> None:
        for provider_name in [name] if name else list(self._state):
            if provider_name in self._state:
                self._state[provider_name] = _State()

    def health(self) -> list[ProviderHealth]:
        now = self._clock()
        report: list[ProviderHealth] = []
        for provider in self.providers():
            state = self._state[provider.name]
            readiness = provider.readiness()
            if not readiness.ok:
                status, detail = readiness.status, readiness.reason
            elif state.cooldown_until > now:
                status, detail = state.status, state.detail
            else:
                status, detail = ProviderStatus.AVAILABLE, provider.description
            report.append(
                ProviderHealth(
                    name=provider.name, kind=provider.kind, tier=provider.tier, status=status, detail=detail,
                    model=provider.model, capabilities=sorted(provider.capabilities),
                    cooldown_seconds=max(0.0, state.cooldown_until - now),
                    consecutive_failures=state.failures, last_success_at=state.last_success_at,
                )  # fmt: skip
            )
        return report

    async def probe_all(self, timeout: float = 8.0) -> list[ProviderHealth]:
        """Live-check every configured provider (used by `doctor` and `providers --probe`)."""

        async def probe_one(provider: BaseProvider) -> None:
            if not provider.readiness().ok:
                return
            try:
                await asyncio.wait_for(provider.probe(), timeout=timeout)
            except ProviderError as exc:
                exc.penalize = True
                self._record_failure(provider, exc)
            except TimeoutError:
                self._record_failure(
                    provider, ProviderError(provider.name, ProviderStatus.NETWORK_ERROR, "probe timed out")
                )
            else:
                self._record_success(provider)

        await asyncio.gather(*(probe_one(p) for p in self._providers))
        return self.health()

    async def aclose(self) -> None:
        await asyncio.gather(*(p.aclose() for p in self._providers), return_exceptions=True)
