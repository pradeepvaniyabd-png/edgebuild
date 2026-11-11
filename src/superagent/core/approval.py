"""Human approval gate.

Agents may *plan* risky operations freely; executing one goes through ``ensure``,
which returns only when a human (or an explicit standing policy) has said yes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import Settings
from ..db.repo import Repo
from ..errors import ApprovalDenied, ApprovalRequired
from ..observability import get_logger
from ..security.secrets import mask_obj
from ..types import ApprovalStatus, RiskLevel
from ..utils import stable_hash
from .events import EventBus

log = get_logger("approval")

# (approved, scope) where scope is "once" or "execution"
Prompt = Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]]


class ApprovalManager:
    def __init__(
        self,
        repo: Repo,
        settings: Settings,
        events: EventBus | None = None,
        *,
        prompt: Prompt | None = None,
        blocking: bool = False,
    ):
        self.repo = repo
        self.settings = settings
        self.events = events
        self.prompt = prompt
        # blocking=True: a long-lived process (API server) keeps the task parked until decided.
        self.blocking = blocking
        self._signals: dict[str, asyncio.Event] = {}

    @staticmethod
    def fingerprint(action: str, details: dict[str, Any]) -> str:
        return stable_hash({"action": action, "details": details}, 40)

    async def ensure(
        self,
        *,
        action: str,
        category: str,
        reason: str,
        details: dict[str, Any],
        risk: RiskLevel = RiskLevel.HIGH,
        execution_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        """Return if approved. Raise ``ApprovalRequired`` (pending) or ``ApprovalDenied``."""
        standing = self.settings.auto_approve_set
        if category in standing or action in standing:
            log.info("auto-approved %s (standing policy SAX_AUTO_APPROVE)", action)
            return
        if execution_id and await self.repo.category_approved_for_execution(execution_id, category):
            return

        fingerprint = self.fingerprint(action, details)
        existing = await self.repo.find_approval(fingerprint, execution_id)
        if existing:
            status = existing["status"]
            if status == ApprovalStatus.APPROVED.value:
                return
            if status == ApprovalStatus.DENIED.value:
                raise ApprovalDenied(f"{action} was denied by {existing.get('decided_by') or 'the user'}")
            raise ApprovalRequired(existing["id"], action, reason)

        approval = await self.repo.create_approval(
            execution_id=execution_id, task_id=task_id, action=action, category=category,
            risk=int(risk), reason=reason, details=mask_obj(details), fingerprint=fingerprint,
        )  # fmt: skip
        if self.events and execution_id:
            await self.events.emit(execution_id, "approval.requested", _public(approval), task_id)

        if self.prompt is not None and self.settings.approval_mode == "interactive":
            approved, scope = await self.prompt(approval)
            await self.decide(approval["id"], approved, decided_by="interactive-user", scope=scope)
            if approved:
                return
            raise ApprovalDenied(f"{action} was denied interactively")
        raise ApprovalRequired(approval["id"], action, reason)

    async def decide(
        self, approval_id: str, approve: bool, *, decided_by: str = "user", scope: str = "once"
    ) -> dict[str, Any] | None:
        status = ApprovalStatus.APPROVED if approve else ApprovalStatus.DENIED
        approval = await self.repo.decide_approval(approval_id, status, decided_by, scope)
        if approval is None:
            return None
        signal = self._signals.get(approval_id)
        if signal:
            signal.set()
        if self.events and approval.get("execution_id"):
            await self.events.emit(
                approval["execution_id"], "approval.resolved", _public(approval), approval.get("task_id")
            )
        return approval

    async def wait(self, approval_id: str, cancel: asyncio.Event | None = None) -> ApprovalStatus:
        """Block until the approval is decided. Also notices decisions made by another process."""
        signal = self._signals.setdefault(approval_id, asyncio.Event())
        try:
            while True:
                approval = await self.repo.get_approval(approval_id)
                if approval is None:
                    return ApprovalStatus.DENIED
                if approval["status"] != ApprovalStatus.PENDING.value:
                    return ApprovalStatus(approval["status"])
                if cancel is not None and cancel.is_set():
                    return ApprovalStatus.PENDING
                try:
                    await asyncio.wait_for(signal.wait(), timeout=2.0)
                except TimeoutError:
                    pass
                signal.clear()
        finally:
            self._signals.pop(approval_id, None)


def _public(approval: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "action", "category", "risk", "reason", "details", "status", "scope", "decided_by")
    return {key: approval.get(key) for key in keys}
