"""Self-healing: classify a failure, pick a *different* way to try again, or escalate.

The engine never schedules an attempt identical to one that already failed.
Each plan carries modifications (longer timeout, excluded provider, repair hint,
new seed ...) that agents and the provider router honour on the next attempt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..errors import (
    ApprovalDenied,
    BudgetExceeded,
    NoProviderAvailable,
    ProviderError,
    SecurityError,
    SuperAgentError,
    classify_error,
)
from ..types import ErrorClass
from ..utils import stable_hash, truncate_middle
from .dag import GraphError, Task

_REMEDIATION: dict[ErrorClass, str] = {
    ErrorClass.TIMEOUT: "raise the task timeout or split the work into smaller steps",
    ErrorClass.NETWORK: "check connectivity / proxy settings and retry",
    ErrorClass.AUTH: "fix or replace the provider credential, then resume the execution",
    ErrorClass.RATE_LIMIT: "wait for the provider limit to reset or configure a second provider",
    ErrorClass.VALIDATION: "inspect the rejected output and tighten the task input",
    ErrorClass.FILE: "check that the path exists inside the workspace and is readable",
    ErrorClass.CODE: "read the captured output, fix the code and resume",
    ErrorClass.DEPENDENCY: "install or configure the missing dependency (see `super-agent doctor`)",
    ErrorClass.MODEL: "configure another model/provider for this capability",
    ErrorClass.MEDIA: "inspect the source media; regenerate or re-encode it",
    ErrorClass.TOOL: "check the tool arguments and permissions",
    ErrorClass.UNKNOWN: "inspect the logs for this task (`super-agent tasks <id>`)",
}


class RecoveryAction(StrEnum):
    RETRY_MODIFIED = "RETRY_MODIFIED"
    WAIT_RETRY = "WAIT_RETRY"
    SWITCH_PROVIDER = "SWITCH_PROVIDER"
    FAIL = "FAIL"


@dataclass
class RecoveryPlan:
    action: RecoveryAction
    error_class: ErrorClass
    reason: str
    delay: float = 0.0
    modifications: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    @property
    def retries(self) -> bool:
        return self.action is not RecoveryAction.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "error_class": self.error_class.value,
            "reason": self.reason,
            "delay": self.delay,
            "modifications": self.modifications,
            "remediation": self.remediation,
        }


class SelfHealingEngine:
    def __init__(self, *, base_delay: float = 2.0, max_delay: float = 60.0):
        self.base_delay = base_delay
        self.max_delay = max_delay

    def plan(self, task: Task, error: BaseException) -> RecoveryPlan:
        error_class = classify_error(error)
        attempt = task.retry_count
        recovery: dict[str, Any] = dict(task.metadata.get("recovery") or {})

        def fail(reason: str) -> RecoveryPlan:
            hint = error.hint if isinstance(error, SuperAgentError) and error.hint else None
            return RecoveryPlan(
                RecoveryAction.FAIL, error_class, reason, remediation=hint or _REMEDIATION[error_class]
            )

        # Decisions a retry must never override.
        if isinstance(error, SecurityError | ApprovalDenied | BudgetExceeded):
            return fail("security, approval and budget decisions are final for this attempt")
        if isinstance(error, GraphError):
            return fail("the plan itself is structurally invalid; re-planning is required")
        if isinstance(error, NoProviderAvailable):
            return fail("no provider is available for this capability")
        if error_class is ErrorClass.DEPENDENCY:
            return fail("a required dependency is missing or not configured")
        if error_class is ErrorClass.FILE:
            return fail("a file problem will not fix itself on retry")
        if attempt >= task.max_retries:
            return fail(f"retry budget exhausted after {attempt} recovery attempt(s)")

        plan = self._strategy(error_class, error, attempt, recovery)

        # Refuse to repeat an attempt we have already made with the same parameters.
        signature = stable_hash({"action": plan.action.value, "mods": plan.modifications, "delay": plan.delay}, 16)
        seen: list[str] = list(task.metadata.get("recovery_signatures") or [])
        if signature in seen:
            return fail("the only remaining recovery strategy was already tried")
        task.metadata["recovery_signatures"] = [*seen, signature]
        return plan

    # ---------------------------------------------------------- strategies
    def _strategy(
        self, error_class: ErrorClass, error: BaseException, attempt: int, recovery: dict[str, Any]
    ) -> RecoveryPlan:
        backoff = min(self.max_delay, self.base_delay * (2**attempt))
        provider = error.provider if isinstance(error, ProviderError) else None
        excluded = sorted({*recovery.get("exclude_providers", []), *([provider] if provider else [])})

        if error_class is ErrorClass.TIMEOUT:
            multiplier = min(4.0, float(recovery.get("timeout_multiplier", 1.0)) * 2)
            return RecoveryPlan(
                RecoveryAction.RETRY_MODIFIED, error_class,
                f"timed out; retrying with a {multiplier:g}x timeout and reduced load",
                modifications={**recovery, "timeout_multiplier": multiplier, "reduce_load": True},
            )  # fmt: skip

        if error_class is ErrorClass.RATE_LIMIT:
            retry_after = error.retry_after if isinstance(error, ProviderError) else None
            if retry_after is not None and retry_after <= 30:
                return RecoveryPlan(
                    RecoveryAction.WAIT_RETRY, error_class,
                    f"rate limited; waiting {retry_after:g}s as the provider asked",
                    delay=float(retry_after), modifications={**recovery, "attempt": attempt + 1},
                )  # fmt: skip
            return RecoveryPlan(
                RecoveryAction.SWITCH_PROVIDER, error_class,
                f"rate limited on {provider or 'provider'}; routing to the next provider",
                modifications={**recovery, "exclude_providers": excluded},
            )  # fmt: skip

        if error_class is ErrorClass.AUTH:
            return RecoveryPlan(
                RecoveryAction.SWITCH_PROVIDER, error_class,
                f"credentials rejected by {provider or 'provider'}; routing to the next provider",
                modifications={**recovery, "exclude_providers": excluded},
                remediation=_REMEDIATION[ErrorClass.AUTH],
            )  # fmt: skip

        if error_class is ErrorClass.NETWORK:
            if attempt == 0:
                return RecoveryPlan(
                    RecoveryAction.WAIT_RETRY, error_class, f"network error; retrying after {backoff:g}s backoff",
                    delay=backoff, modifications={**recovery, "attempt": attempt + 1},
                )  # fmt: skip
            return RecoveryPlan(
                RecoveryAction.SWITCH_PROVIDER, error_class,
                "network error persisted; routing away from the failing endpoint",
                delay=backoff, modifications={**recovery, "exclude_providers": excluded, "attempt": attempt + 1},
            )  # fmt: skip

        if error_class is ErrorClass.MODEL:
            return RecoveryPlan(
                RecoveryAction.SWITCH_PROVIDER, error_class,
                f"{provider or 'model'} could not serve the request; trying another provider",
                modifications={**recovery, "exclude_providers": excluded, "seed_bump": attempt + 1},
            )  # fmt: skip

        if error_class is ErrorClass.VALIDATION:
            return RecoveryPlan(
                RecoveryAction.RETRY_MODIFIED, error_class,
                "output failed validation; retrying with the validation error as a repair hint",
                modifications={**recovery, "repair_hint": truncate_middle(str(error), 1200)},
            )  # fmt: skip

        if error_class is ErrorClass.CODE:
            output = getattr(error, "output", "") or str(error)
            return RecoveryPlan(
                RecoveryAction.RETRY_MODIFIED, error_class,
                "code failed; retrying with the captured failure as debugging context",
                modifications={**recovery, "debug_context": truncate_middle(output, 4000)},
            )  # fmt: skip

        if error_class is ErrorClass.MEDIA:
            return RecoveryPlan(
                RecoveryAction.RETRY_MODIFIED, error_class,
                "media output was invalid; regenerating with a new seed and a clean re-encode",
                modifications={**recovery, "seed_bump": attempt + 1, "reencode": True},
            )  # fmt: skip

        if error_class is ErrorClass.TOOL:
            return RecoveryPlan(
                RecoveryAction.WAIT_RETRY, error_class, f"tool failed; one retry after {backoff:g}s",
                delay=backoff, modifications={**recovery, "attempt": attempt + 1},
            )  # fmt: skip

        return RecoveryPlan(
            RecoveryAction.RETRY_MODIFIED, error_class,
            "unclassified failure; retrying once in safe mode (serial, lower load)",
            modifications={**recovery, "safe_mode": True, "exclude_providers": excluded},
        )  # fmt: skip
