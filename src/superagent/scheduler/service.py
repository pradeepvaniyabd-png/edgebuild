"""Recurring jobs: APScheduler triggers, persisted in the database, executed through the orchestrator."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from ..errors import ValidationFailed
from ..observability import get_logger
from ..utils import utcnow

log = get_logger("scheduler")
_INTERVAL = re.compile(r"^\s*(\d+)\s*(s|m|h|d)\s*$", re.IGNORECASE)
_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


_DOW = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def _cron_dow(field: str) -> str:
    """Standard cron numbers weekdays 0/7=Sunday..6=Saturday; APScheduler counts 0=Monday. Translate to names."""
    if field == "*" or field.startswith("*/"):
        return field
    names: list[str] = []
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, raw_step = part.split("/", 1)
            step = int(raw_step)
        if "-" in part:
            low, high = part.split("-", 1)
            if low.isdigit() and high.isdigit():
                numbers = list(range(int(low), int(high) + 1, step))
            else:
                names.append(f"{part}/{step}" if step != 1 else part)
                continue
        elif part.isdigit():
            numbers = [int(part)]
        else:
            names.append(part.lower())
            continue
        if any(n < 0 or n > 7 for n in numbers):
            raise ValueError(f"day-of-week {part} is out of range 0-7")
        names += [_DOW[n % 7] for n in numbers]
    return ",".join(dict.fromkeys(names))


def build_trigger(kind: str, expression: str, timezone: str):  # noqa: ANN201
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationFailed(f"unknown timezone '{timezone}'", hint="use an IANA name such as Asia/Kolkata") from exc
    if kind == "cron":
        try:
            fields = expression.split()
            if len(fields) != 5:
                raise ValueError("expected 5 fields: minute hour day month weekday")
            minute, hour, day, month, weekday = fields
            return CronTrigger(minute=minute, hour=hour, day=day, month=month, day_of_week=_cron_dow(weekday), timezone=zone)
        except (ValueError, TypeError) as exc:
            raise ValidationFailed(f"invalid cron expression '{expression}': {exc}", hint="five fields, e.g. '30 9 * * 1-5'") from exc
    if kind == "interval":
        match = _INTERVAL.match(expression)
        if not match or int(match.group(1)) <= 0:
            raise ValidationFailed(f"invalid interval '{expression}'", hint="use 30s, 15m, 2h or 1d")
        if int(match.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()] < 60:
            raise ValidationFailed("the shortest allowed interval is 60 seconds")
        return IntervalTrigger(timezone=zone, **{_UNITS[match.group(2).lower()]: int(match.group(1))})
    raise ValidationFailed(f"trigger must be 'cron' or 'interval', not '{kind}'")


class SchedulerService:
    def __init__(self, platform: Any):
        self.p = platform
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self.started = False

    async def start(self) -> None:
        self._scheduler.start()
        self.started = True
        for job in await self.p.repo.list_jobs():
            if job["status"] == "active":
                self._register(job)
                await self._refresh_next(job["id"])

    async def stop(self) -> None:
        if self.started:
            self._scheduler.shutdown(wait=False)
            self.started = False

    def _register(self, job: dict[str, Any]) -> None:
        self._scheduler.add_job(
            self._fire, build_trigger(job["trigger"], job["expression"], job["timezone"]), args=[job["id"]], id=job["id"],
            replace_existing=True, coalesce=True, max_instances=1, misfire_grace_time=3600,
        )  # fmt: skip

    async def _refresh_next(self, job_id: str) -> None:
        scheduled = self._scheduler.get_job(job_id)
        await self.p.repo.update_job(job_id, next_run_at=scheduled.next_run_time if scheduled else None)

    async def add(self, name: str, command: str, *, trigger: str, expression: str, timezone: str = "UTC") -> dict[str, Any]:
        build_trigger(trigger, expression, timezone)  # validate before persisting
        if not command.strip():
            raise ValidationFailed("the scheduled command is empty")
        if await self.p.repo.get_job_by_name(name):
            raise ValidationFailed(f"a job named '{name}' already exists")
        job = await self.p.repo.create_job(name=name, command=command, trigger=trigger, expression=expression, timezone=timezone)
        if self.started:
            self._register(job)
            await self._refresh_next(job["id"])
        return await self.p.repo.get_job(job["id"]) or job

    async def remove(self, job_id: str) -> bool:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        return await self.p.repo.delete_job(job_id)

    async def pause(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id):
            self._scheduler.remove_job(job_id)
        await self.p.repo.update_job(job_id, status="paused", next_run_at=None)

    async def resume(self, job_id: str) -> None:
        job = await self.p.repo.get_job(job_id)
        if job is None:
            raise ValidationFailed(f"unknown job '{job_id}'")
        await self.p.repo.update_job(job_id, status="active")
        if self.started:
            self._register(job)
            await self._refresh_next(job_id)

    async def run_now(self, job_id: str) -> dict[str, Any]:
        return await self._fire(job_id)

    async def list(self) -> list[dict[str, Any]]:
        return await self.p.repo.list_jobs()

    async def _fire(self, job_id: str) -> dict[str, Any]:
        job = await self.p.repo.get_job(job_id)
        if job is None:
            return {"status": "MISSING"}
        started = utcnow()
        try:
            result = await self.p.orchestrator.run(job["command"], source="scheduler")
            status, execution_id = result.status.value, result.execution_id
            failed = result.status.value in {"FAILED", "PARTIAL"}
        except Exception:  # a crashing job must not kill the scheduler loop
            log.exception("scheduled job %s crashed", job["name"])
            status, execution_id, failed = "CRASHED", None, True
        await self.p.repo.update_job(
            job_id, last_run_at=started, last_status=status, last_execution_id=execution_id,
            run_count=job["run_count"] + 1, failure_count=job["failure_count"] + 1 if failed else 0,
        )  # fmt: skip
        await self._refresh_next(job_id)
        return {"status": status, "execution_id": execution_id}


def next_run_preview(kind: str, expression: str, timezone: str, count: int = 3) -> list[datetime]:
    trigger = build_trigger(kind, expression, timezone)
    now, out, previous = datetime.now(ZoneInfo(timezone)), [], None
    for _ in range(count):
        nxt = trigger.get_next_fire_time(previous, previous or now)
        if nxt is None:
            break
        out.append(nxt)
        previous = nxt
    return out
