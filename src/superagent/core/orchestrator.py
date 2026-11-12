"""Master orchestrator: command → intent → plan → parallel execution → verification → result.

``Platform`` owns every long-lived component. ``Orchestrator`` runs executions on it and
is the only place that knows the whole pipeline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..agents import AgentRegistry, build_agents
from ..agents.base import AgentContext, BoundTools
from ..config import Settings, get_settings
from ..db.repo import Repo
from ..db.session import Database
from ..errors import SuperAgentError, VerificationFailed
from ..memory.manager import MemoryManager
from ..observability import configure_logging, get_logger, log_context
from ..providers.registry import build_router
from ..providers.router import ProviderRouter
from ..providers.services import AI
from ..security import CommandPolicy, PathSandbox
from ..tools import ToolContext, ToolRegistry, build_registry
from ..types import ExecutionStatus, TaskStatus
from ..utils import new_id, truncate_middle, utcnow
from .approval import ApprovalManager, Prompt
from .context import ContextManager
from .cost import CostEngine
from .dag import Task, TaskGraph
from .events import TERMINAL_EVENT, EventBus
from .executor import ParallelExecutor, TaskOutcome
from .healing import SelfHealingEngine
from .intent import IntentEngine, IntentResult
from .planner import Plan, Planner
from .verification import VerificationEngine

log = get_logger("orchestrator")


class Platform:
    """All long-lived services, wired once."""

    def __init__(self, settings: Settings, db: Database, router: ProviderRouter, *, approval_prompt: Prompt | None = None,
                 blocking_approvals: bool = False, extra_agents: list[Any] | None = None):  # fmt: skip
        self.settings = settings
        self.db = db
        self.repo = Repo(db)
        self.router = router
        self.events = EventBus(self.repo)
        self.approvals = ApprovalManager(self.repo, settings, self.events, prompt=approval_prompt, blocking=blocking_approvals)
        self.cost = CostEngine(self.repo, settings, self.approvals)
        self.ai = AI(router, self.cost)
        self.memory = MemoryManager(self.repo, settings, self.ai)
        self.sandbox = PathSandbox([settings.workspace], settings.read_roots)
        self.policy = CommandPolicy(self.sandbox, [s.strip() for s in settings.shell_allow.split(",") if s.strip()])
        self.tools: ToolRegistry = build_registry()
        self.agents: AgentRegistry = build_agents(extra_agents)
        self.context = ContextManager(self.memory, default_budget=settings.context_token_budget)
        self.verifier = VerificationEngine()
        self.healing = SelfHealingEngine()
        self.intents = IntentEngine(self.ai)
        self.creative: Any = None  # set by the creative package when installed
        self.scheduler: Any = None
        self.orchestrator = Orchestrator(self)

    @classmethod
    async def create(cls, settings: Settings | None = None, *, configure_log: bool = True, **kwargs: Any) -> Platform:
        settings = settings or get_settings()
        settings.ensure_dirs()
        if configure_log:
            configure_logging(settings)
        db = Database(settings.db_url)
        await db.ensure_ready(settings.should_auto_migrate)
        router = await build_router(settings)
        platform = cls(settings, db, router, **kwargs)
        try:
            from ..creative import install as install_creative

            install_creative(platform)
        except ImportError:  # the creative pipeline is optional at import time
            log.info("creative pipeline not installed")
        interrupted = await platform.repo.mark_interrupted()
        if interrupted:
            log.warning("%d execution(s) were interrupted by a restart; resume them with `super-agent resume <id>`", interrupted)
        return platform

    def planner(self) -> Planner:
        return Planner(self.agents, self.settings, self.ai, self.creative)

    async def aclose(self) -> None:
        await self.orchestrator.shutdown()
        if self.scheduler is not None:
            await self.scheduler.stop()
        await self.router.aclose()
        await self.db.dispose()


@dataclass
class ExecutionResult:
    execution_id: str
    status: ExecutionStatus
    command: str
    intent: IntentResult | None = None
    final_text: str = ""
    tasks: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    approvals: list[dict[str, Any]] = field(default_factory=list)
    plan_notes: list[str] = field(default_factory=list)
    final_ready: bool = False
    gates: dict[str, bool] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id, "status": self.status.value, "command": self.command,
            "intent": self.intent.model_dump(mode="json") if self.intent else None, "final_text": self.final_text,
            "tasks": self.tasks, "artifacts": self.artifacts, "warnings": self.warnings, "approvals": self.approvals,
            "plan_notes": self.plan_notes, "final_ready": self.final_ready, "gates": self.gates, "error": self.error,
        }  # fmt: skip


class Orchestrator:
    def __init__(self, platform: Platform):
        self.p = platform
        self._cancels: dict[str, asyncio.Event] = {}
        self._background: dict[str, asyncio.Task[ExecutionResult]] = {}

    # ------------------------------------------------------------------ public
    async def run(self, command: str, *, source: str = "cli", project_id: str | None = None) -> ExecutionResult:
        execution_id = await self._open(command, source, project_id)
        return await self._run(execution_id, command, project_id)

    async def run_plan(self, command: str, tasks: list[Task], *, source: str = "cli", project_id: str | None = None) -> ExecutionResult:
        """Run an explicit task list (used by the project CLI/API) through the same pipeline, skipping planning."""
        execution_id = await self._open(command, source, project_id)
        return await self._run(execution_id, command, project_id, tasks)

    async def submit(self, command: str, *, source: str = "api", project_id: str | None = None, tasks: list[Task] | None = None) -> str:
        """Start an execution in the background and return its id immediately."""
        execution_id = await self._open(command, source, project_id)
        task = asyncio.create_task(self._run(execution_id, command, project_id, tasks), name=f"execution:{execution_id}")
        self._background[execution_id] = task
        task.add_done_callback(lambda _t, eid=execution_id: self._background.pop(eid, None))
        return execution_id

    async def resume(self, execution_id: str, *, wait: bool = True) -> ExecutionResult | str:
        row = await self.p.repo.get_execution(execution_id)
        if row is None:
            raise SuperAgentError(f"unknown execution '{execution_id}'")
        if execution_id in self._background:
            raise SuperAgentError("that execution is still running")
        coroutine = self._resume(row)
        if wait:
            return await coroutine
        task = asyncio.create_task(coroutine, name=f"resume:{execution_id}")
        self._background[execution_id] = task
        task.add_done_callback(lambda _t, eid=execution_id: self._background.pop(eid, None))
        return execution_id

    def cancel(self, execution_id: str) -> bool:
        event = self._cancels.get(execution_id)
        if event is None:
            return False
        event.set()
        return True

    async def wait(self, execution_id: str) -> ExecutionResult | None:
        task = self._background.get(execution_id)
        return await task if task else None

    async def shutdown(self) -> None:
        for event in self._cancels.values():
            event.set()
        pending = list(self._background.values())
        if pending:
            await asyncio.wait(pending, timeout=10)

    # ---------------------------------------------------------------- pipeline
    async def _open(self, command: str, source: str, project_id: str | None) -> str:
        if not command.strip():
            raise SuperAgentError("the command is empty")
        execution_id = new_id("exe")
        await self.p.repo.create_execution(id=execution_id, command=command, source=source, project_id=project_id)
        self._cancels[execution_id] = asyncio.Event()
        await self.p.events.emit(execution_id, "execution.created", {"command": truncate_middle(command, 500), "source": source})
        return execution_id

    async def _run(self, execution_id: str, command: str, project_id: str | None, tasks: list[Task] | None = None) -> ExecutionResult:
        repo, events = self.p.repo, self.p.events
        intent: IntentResult | None = None
        plan_notes: list[str] = []
        try:
            with log_context(execution_id=execution_id, agent="master"):
                intent = self.p.intents.classify(command) if tasks else await self.p.intents.detect(command)
                await repo.update_execution(execution_id, intent=intent.model_dump(mode="json"))
                await events.emit(execution_id, "intent.detected", intent.model_dump(mode="json"))
                plan: Plan = Plan(tasks=tasks, method="explicit") if tasks else await self.p.planner().plan(command, intent)
                plan_notes = plan.notes
                graph = plan.graph()
                await repo.save_tasks(execution_id, graph.tasks)
                await events.emit(execution_id, "plan.created", {
                    "method": plan.method, "notes": plan.notes, "waves": [[t.id for t in wave] for wave in graph.levels()],
                    "tasks": [{"id": t.id, "agent": t.agent, "action": t.action, "description": t.description,
                               "dependencies": t.dependencies} for t in graph.tasks],
                })  # fmt: skip
                await repo.update_execution(execution_id, status=ExecutionStatus.RUNNING.value, started_at=utcnow())
                return await self._drive(execution_id, command, graph, intent, plan_notes, project_id)
        except SuperAgentError as exc:
            return await self._abort(execution_id, command, intent, plan_notes, exc)
        except Exception as exc:  # last resort: never leave an execution stuck in RUNNING
            log.exception("execution %s crashed", execution_id)
            return await self._abort(execution_id, command, intent, plan_notes, exc)
        finally:
            self._cancels.pop(execution_id, None)
            self.p.memory.forget_execution(execution_id)

    async def _resume(self, row: dict[str, Any]) -> ExecutionResult:
        execution_id = row["id"]
        self._cancels[execution_id] = asyncio.Event()
        repo = self.p.repo
        try:
            graph = TaskGraph.from_rows(await repo.load_tasks(execution_id))
            reset = graph.reset_for_resume()
            intent = IntentResult(**row["intent"]) if row.get("intent") else None
            await repo.save_tasks(execution_id, graph.tasks)
            await repo.update_execution(execution_id, status=ExecutionStatus.RUNNING.value, error=None, completed_at=None)
            await self.p.events.emit(execution_id, "execution.resumed", {"requeued": [t.id for t in reset], "kept": len(graph) - len(reset)})
            return await self._drive(execution_id, row["command"], graph, intent, [], row.get("project_id"))
        except SuperAgentError as exc:
            return await self._abort(execution_id, row["command"], None, [], exc)
        finally:
            self._cancels.pop(execution_id, None)
            self.p.memory.forget_execution(execution_id)

    async def _drive(self, execution_id: str, command: str, graph: TaskGraph, intent: IntentResult | None,
                     plan_notes: list[str], project_id: str | None) -> ExecutionResult:  # fmt: skip
        cancel = self._cancels.setdefault(execution_id, asyncio.Event())
        language = intent.language if intent else "en"
        persisted: set[str] = set()

        async def handler(task: Task, resolved: dict[str, Any]) -> TaskOutcome:
            return await self._run_task(execution_id, command, graph, task, resolved, language, project_id)

        async def listener(task: Task, event: str, data: dict[str, Any]) -> None:
            await self.p.repo.save_task(execution_id, task)
            payload = {"agent": task.agent, "action": task.action, "description": task.description, "status": task.status.value, **data}
            if event == "task.succeeded":
                payload["summary"] = task.metadata.get("summary", "")
            await self.p.events.emit(execution_id, event, payload, task.id)
            persisted.add(task.id)

        executor = ParallelExecutor(handler, healing=self.p.healing, approvals=self.p.approvals,
                                    max_concurrency=self.p.settings.max_concurrency, listener=listener)  # fmt: skip
        await executor.run(graph, cancel)
        await self.p.repo.save_tasks(execution_id, graph.tasks)  # final state of every task, including never-started ones
        return await self._finalize(execution_id, command, graph, intent, plan_notes, cancelled=cancel.is_set())

    async def _run_task(self, execution_id: str, command: str, graph: TaskGraph, task: Task, resolved: dict[str, Any],
                        language: str, project_id: str | None) -> TaskOutcome:  # fmt: skip
        p = self.p
        agent = p.agents.get(task.agent)
        recovery = task.metadata.get("recovery") or {}
        project = task.metadata.get("project_id") or project_id
        ai = p.ai.scoped(execution_id=execution_id, task_id=task.id, project_id=project, exclude=recovery.get("exclude_providers", ()))
        tool_ctx = ToolContext(
            settings=p.settings, sandbox=p.sandbox, policy=p.policy, approvals=p.approvals, events=p.events, ai=ai, repo=p.repo,
            memory=p.memory, execution_id=execution_id, task_id=task.id, project_id=project, agent=agent.name, permission=agent.permissions,
        )  # fmt: skip
        tool_lines = [f"{name}: {spec.description}" for name in p.tools.names() if (spec := p.tools.get(name)).permission_level <= agent.permissions][:30]
        pack = await p.context.build(
            task=task, graph=graph, command=command, execution_id=execution_id, agent_description=f"{agent.name}: {agent.description}",
            tool_summaries=tool_lines, resolved_input=resolved, language=language,
        )  # fmt: skip
        actx = AgentContext(
            settings=p.settings, execution_id=execution_id, task=task, input=resolved, tools=BoundTools(p.tools, tool_ctx), ai=ai,
            memory=p.memory, repo=p.repo, events=p.events, context=pack, command=command, language=language, recovery=recovery,
            project_id=project, services=p,
        )  # fmt: skip
        with log_context(agent=agent.name, task_id=task.id):
            result = await agent.execute(actx)
            specs = [*task.metadata.get("verify", []), *result.verify]
            report = await p.verifier.verify(specs, result.output)
        task.metadata["verification"] = report.to_dict()
        if not report.passed:
            raise VerificationFailed(f"{agent.name}.{task.action} result failed verification: {report.summary()}",
                                     [c.name for c in report.failures])  # fmt: skip
        task.metadata["summary"] = result.summary
        task.metadata["artifacts"] = result.artifacts
        task.metadata["warnings"] = result.warnings
        p.memory.working(execution_id).set(task.id, result.summary or truncate_middle(str(result.output), 400))
        p.memory.short_term(execution_id).add(agent.name, result.summary)
        return TaskOutcome(output=result.output, spawned=result.spawn)

    async def _abort(self, execution_id: str, command: str, intent: IntentResult | None, notes: list[str], exc: BaseException) -> ExecutionResult:
        message = f"{type(exc).__name__}: {exc}"
        await self.p.repo.update_execution(execution_id, status=ExecutionStatus.FAILED.value, error=message, completed_at=utcnow())
        await self.p.events.emit(execution_id, TERMINAL_EVENT, {"status": ExecutionStatus.FAILED.value, "error": message})
        return ExecutionResult(execution_id, ExecutionStatus.FAILED, command, intent, plan_notes=notes, error=message,
                               final_text=message)  # fmt: skip

    async def _finalize(self, execution_id: str, command: str, graph: TaskGraph, intent: IntentResult | None,
                        plan_notes: list[str], *, cancelled: bool) -> ExecutionResult:  # fmt: skip
        counts = graph.counts()
        tasks = graph.tasks
        success = [t for t in tasks if t.status is TaskStatus.SUCCESS]
        waiting = [t for t in tasks if t.status is TaskStatus.WAITING_APPROVAL]
        if cancelled:
            status = ExecutionStatus.CANCELLED
        elif waiting:
            status = ExecutionStatus.WAITING_APPROVAL
        elif len(success) == len(tasks):
            status = ExecutionStatus.SUCCESS
        elif success:
            status = ExecutionStatus.PARTIAL
        else:
            status = ExecutionStatus.FAILED

        artifacts: list[str] = []
        warnings: list[str] = list(plan_notes)
        rows: list[dict[str, Any]] = []
        for task in tasks:
            artifacts += [a for a in task.metadata.get("artifacts", []) if a not in artifacts]
            warnings += [w for w in task.metadata.get("warnings", []) if w not in warnings]
            verification = task.metadata.get("verification")
            rows.append({
                "id": task.id, "agent": task.agent, "action": task.action, "description": task.description,
                "status": task.status.value, "summary": task.metadata.get("summary", ""), "error": task.error,
                "retries": task.retry_count, "verification": verification,
                "failure": task.metadata.get("failure"),
            })  # fmt: skip
        synth = next((t for t in reversed(tasks) if t.agent == "master" and t.action in {"synthesize", "answer"} and t.output), None)
        final_text = (synth.output or {}).get("text", "") if synth else ""
        if not final_text:
            final_text = "\n".join(f"- {t.metadata['summary']}" for t in success if t.metadata.get("summary"))
        approvals = [a for a in await self.p.repo.list_approvals(status="PENDING", execution_id=execution_id)]

        gates = {
            "tasks_succeeded": status is ExecutionStatus.SUCCESS,
            "verification_passed": all((t.metadata.get("verification") or {}).get("passed", True) for t in success),
            "dependencies_satisfied": not any(t.status is TaskStatus.BLOCKED for t in tasks),
            "no_pending_approvals": not approvals,
        }  # fmt: skip
        final_ready = all(gates.values())
        error = None
        if status in (ExecutionStatus.FAILED, ExecutionStatus.PARTIAL):
            failed = next((t for t in tasks if t.status is TaskStatus.FAILED), None)
            error = failed.error if failed else None
        await self.p.repo.update_execution(
            execution_id, status=status.value, completed_at=utcnow(), error=error,
            result={"final_text": final_text, "artifacts": artifacts, "counts": counts, "final_ready": final_ready, "gates": gates},
        )  # fmt: skip
        await self.p.events.emit(execution_id, TERMINAL_EVENT, {"status": status.value, "final_ready": final_ready, "counts": counts})
        return ExecutionResult(
            execution_id, status, command, intent, final_text, rows, artifacts, warnings,
            [{k: a[k] for k in ("id", "action", "category", "reason")} for a in approvals], plan_notes, final_ready, gates, error,
        )  # fmt: skip


# ------------------------------------------------------------------ presentation
_STATUS_LINE = {
    ExecutionStatus.SUCCESS: "COMPLETE", ExecutionStatus.PARTIAL: "PARTIAL", ExecutionStatus.FAILED: "FAILED",
    ExecutionStatus.WAITING_APPROVAL: "WAITING FOR APPROVAL", ExecutionStatus.CANCELLED: "CANCELLED",
}  # fmt: skip


def format_result(result: ExecutionResult) -> str:
    """The user-facing report: STATUS, WHAT WAS DONE, OUTPUTS, QUALITY CHECK, WARNINGS, NEXT ACTION."""
    lines = [f"STATUS: {_STATUS_LINE.get(result.status, result.status.value)}  ({result.execution_id})", ""]
    lines.append("WHAT WAS DONE")
    for task in result.tasks:
        mark = {"SUCCESS": "✔", "FAILED": "✘", "BLOCKED": "–", "WAITING_APPROVAL": "⏸", "CANCELLED": "–"}.get(task["status"], "·")
        detail = task["summary"] or task["error"] or task["status"].lower()
        retries = f" (after {task['retries']} recovery attempt(s))" if task["retries"] else ""
        lines.append(f"  {mark} {task['agent']}.{task['action']}: {truncate_middle(str(detail), 160)}{retries}")
    if result.final_text:
        lines += ["", "RESULT", truncate_middle(result.final_text, 3000)]
    lines += ["", "OUTPUTS"]
    lines += [f"  {path}" for path in result.artifacts] or ["  (no files)"]
    checked = [t for t in result.tasks if t["verification"]]
    lines += ["", "QUALITY CHECK"]
    if checked:
        passed = sum(1 for t in checked if t["verification"]["passed"])
        lines.append(f"  {passed}/{len(checked)} verified task(s) passed all checks; final-ready: {'yes' if result.final_ready else 'no'}")
    else:
        lines.append(f"  no verification checks ran; final-ready: {'yes' if result.final_ready else 'no'}")
    if result.warnings:
        lines += ["", "WARNINGS"] + [f"  - {w}" for w in result.warnings[:12]]
    lines += ["", "NEXT ACTION"]
    if result.approvals:
        for approval in result.approvals:
            lines.append(f"  approve or deny: super-agent approvals decide {approval['id']} --approve   ({approval['reason']})")
        lines.append(f"  then: super-agent resume {result.execution_id}")
    elif result.status in (ExecutionStatus.FAILED, ExecutionStatus.PARTIAL):
        remediation = next((t["failure"]["remediation"] for t in result.tasks if t.get("failure") and t["failure"].get("remediation")), None)
        lines.append(f"  {remediation or 'inspect the failed task'}; then: super-agent resume {result.execution_id}")
    else:
        lines.append("  none")
    return "\n".join(lines)
