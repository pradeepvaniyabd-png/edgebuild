"""Cost engine: estimate before spending, enforce budgets, record what was used.

Local and free-tier providers cost nothing and are never gated. A paid provider call
whose estimate would exceed the per-generation, project or daily budget needs human
approval first. Providers without a known price are charged a configurable stand-in
estimate so budgets still bite; such records are flagged ``priced=False``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..db.repo import Repo
from ..errors import BudgetExceeded, ConfigError
from ..observability import get_logger
from ..types import ProviderTier, RiskLevel
from ..utils import utcnow
from .approval import ApprovalManager

log = get_logger("cost")

# Anthropic first-party list prices in USD per million tokens, as of 2026-11-03.
# Everything else is unpriced until the user supplies SAX_PRICES_FILE.
BUILTIN_PRICES: dict[str, dict[str, dict[str, float]]] = {
    "anthropic": {
        "claude-fable-5-2": {"input_per_mtok": 8.0, "output_per_mtok": 40.0},
        "claude-opus-5-5": {"input_per_mtok": 4.0, "output_per_mtok": 20.0},
        "claude-opus-5": {"input_per_mtok": 5.0, "output_per_mtok": 25.0},
        "claude-sonnet-5-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0},
        "claude-sonnet-5": {"input_per_mtok": 2.0, "output_per_mtok": 10.0},
        "claude-haiku-5-5": {"input_per_mtok": 0.10, "output_per_mtok": 0.50},
        "claude-haiku-4-5": {"input_per_mtok": 1.0, "output_per_mtok": 5.0},
    }
}


@dataclass(frozen=True)
class Estimate:
    usd: float
    priced: bool
    basis: str


class CostEngine:
    def __init__(self, repo: Repo, settings: Settings, approvals: ApprovalManager | None = None):
        self.repo = repo
        self.settings = settings
        self.approvals = approvals
        self.prices = self._load_prices()

    def _load_prices(self) -> dict[str, dict[str, dict[str, float]]]:
        prices = {provider: dict(models) for provider, models in BUILTIN_PRICES.items()}
        path = self.settings.prices_file
        if path is None:
            return prices
        try:
            custom = json.loads(path.expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read SAX_PRICES_FILE ({path}): {exc}") from exc
        for provider, models in custom.items():
            prices.setdefault(provider, {}).update(models)
        return prices

    # ------------------------------------------------------------- estimate
    def _price(self, provider: str, model: str | None) -> dict[str, float] | None:
        models = self.prices.get(provider) or {}
        return models.get(model or "") or models.get("*")

    def estimate(self, *, provider: str, model: str | None, kind: str, units: dict[str, Any], tier: ProviderTier) -> Estimate:
        if not tier.is_paid:
            return Estimate(0.0, True, "local or free tier")
        price = self._price(provider, model)
        if price is not None:
            usd = _apply(price, kind, units)
            if usd is not None:
                return Estimate(usd, True, "price table")
        return Estimate(self.settings.unpriced_estimate_usd, False, "no price on file; stand-in estimate")

    # ------------------------------------------------------------ authorise
    async def authorize(
        self,
        *,
        provider: str,
        model: str | None,
        kind: str,
        units: dict[str, Any],
        tier: ProviderTier,
        execution_id: str | None = None,
        project_id: str | None = None,
        task_id: str | None = None,
    ) -> Estimate:
        estimate = self.estimate(provider=provider, model=model, kind=kind, units=units, tier=tier)
        if not tier.is_paid or estimate.usd <= 0:
            return estimate
        breach = await self._breach(estimate.usd, project_id)
        if breach is None:
            return estimate
        if self.approvals is None:
            raise breach
        await self.approvals.ensure(
            action=f"paid.{kind}",
            category="paid",
            reason=f"{breach.message} [{provider}{'/' + model if model else ''}; {estimate.basis}]",
            details={"provider": provider, "model": model, "kind": kind, "scope": breach.scope,
                     "limit_usd": breach.limit, "task_id": task_id},  # fmt: skip
            risk=RiskLevel.HIGH,
            execution_id=execution_id,
            task_id=task_id,
        )
        log.info("paid %s on %s approved above the %s budget", kind, provider, breach.scope)
        return estimate

    async def _breach(self, usd: float, project_id: str | None) -> BudgetExceeded | None:
        settings = self.settings
        if usd > settings.per_generation_budget_usd:
            return BudgetExceeded("per-generation", usd, settings.per_generation_budget_usd, 0.0)
        if project_id:
            spent = await self.repo.cost_sum(project_id=project_id)
            if spent + usd > settings.project_budget_usd:
                return BudgetExceeded("project", usd, settings.project_budget_usd, spent)
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        spent_today = await self.repo.cost_sum(since=day_start)
        if spent_today + usd > settings.daily_budget_usd:
            return BudgetExceeded("daily", usd, settings.daily_budget_usd, spent_today)
        return None

    # --------------------------------------------------------------- record
    async def record(
        self,
        *,
        provider: str,
        model: str | None,
        kind: str,
        units: dict[str, Any],
        tier: ProviderTier,
        estimate: Estimate | None = None,
        execution_id: str | None = None,
        project_id: str | None = None,
    ) -> float:
        """Store what was actually used. Returns the recorded cost in USD."""
        actual = self.estimate(provider=provider, model=model, kind=kind, units=units, tier=tier)
        try:
            await self.repo.add_cost(
                execution_id=execution_id, project_id=project_id, provider=provider, model=model, kind=kind,
                units=units, estimated_usd=(estimate or actual).usd,
                actual_usd=actual.usd if actual.priced else None, priced=actual.priced,
            )  # fmt: skip
        except Exception:  # accounting must not break the work it accounts for
            log.warning("could not record cost for %s/%s", provider, kind, exc_info=True)
        return actual.usd

    async def summary(self, project_id: str | None = None) -> dict[str, Any]:
        day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        records = await self.repo.list_costs(limit=2000, project_id=project_id)
        by_provider: dict[str, dict[str, Any]] = {}
        for record in records:
            bucket = by_provider.setdefault(record["provider"], {"calls": 0, "usd": 0.0, "unpriced_calls": 0})
            bucket["calls"] += 1
            bucket["usd"] += record["actual_usd"] if record["actual_usd"] is not None else record["estimated_usd"]
            bucket["unpriced_calls"] += 0 if record["priced"] else 1
        for bucket in by_provider.values():
            bucket["usd"] = round(bucket["usd"], 6)
        return {
            "today_usd": round(await self.repo.cost_sum(since=day_start), 6),
            "total_usd": round(await self.repo.cost_sum(project_id=project_id), 6),
            "budgets": {
                "daily_usd": self.settings.daily_budget_usd,
                "project_usd": self.settings.project_budget_usd,
                "per_generation_usd": self.settings.per_generation_budget_usd,
            },
            "by_provider": by_provider,
            "note": "unpriced calls use a stand-in estimate; add real prices via SAX_PRICES_FILE",
        }


def _apply(price: dict[str, float], kind: str, units: dict[str, Any]) -> float | None:
    if kind in ("llm", "embedding") and "input_per_mtok" in price:
        usd = float(units.get("input_tokens", 0)) / 1e6 * price["input_per_mtok"]
        return usd + float(units.get("output_tokens", 0)) / 1e6 * price.get("output_per_mtok", 0.0)
    if kind == "image" and "per_image" in price:
        return float(units.get("images", 1)) * price["per_image"]
    if kind == "video":
        if "per_second" in price:
            return float(units.get("seconds", 0)) * price["per_second"]
        if "per_video" in price:
            return price["per_video"]
    if kind == "voice" and "per_1k_chars" in price:
        return float(units.get("characters", 0)) / 1000 * price["per_1k_chars"]
    return None
