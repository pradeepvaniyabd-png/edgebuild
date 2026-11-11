from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from superagent.core.dag import GraphError, Task, TaskGraph
from superagent.core.executor import ParallelExecutor, TaskOutcome
from superagent.core.healing import RecoveryAction, SelfHealingEngine
from superagent.errors import (
    ApprovalRequired,
    DependencyMissing,
    MediaError,
    PathEscapeError,
    ProviderError,
    ValidationFailed,
)
from superagent.types import ErrorClass, ProviderStatus, TaskStatus


def T(tid: str, deps: list[str] | None = None, **kw: Any) -> Task:
    return Task(id=tid, description=tid, agent="test", action="run", dependencies=deps or [], **kw)


FAST = SelfHealingEngine(base_delay=0.01, max_delay=0.02)


# ---------------------------------------------------------------------- graph
def test_cycle_unknown_dependency_and_duplicates_are_rejected():
    with pytest.raises(GraphError, match="cycle"):
        TaskGraph([T("a", ["b"]), T("b", ["a"])])
    with pytest.raises(GraphError, match="unknown"):
        TaskGraph([T("a", ["ghost"])])
    with pytest.raises(GraphError, match="itself"):
        TaskGraph([T("a", ["a"])])
    graph = TaskGraph([T("a")])
    with pytest.raises(GraphError, match="duplicate"):
        graph.add(T("a"))


def test_add_many_is_atomic_on_error():
    graph = TaskGraph([T("a")])
    with pytest.raises(GraphError):
        graph.add_many([T("b", ["a"]), T("c", ["missing"])])
    assert [t.id for t in graph] == ["a"]


def test_levels_expose_parallel_waves():
    graph = TaskGraph([T("a"), T("b"), T("c", ["a", "b"]), T("d", ["c"]), T("e", ["a"])])
    assert [[t.id for t in wave] for wave in graph.levels()] == [["a", "b"], ["c", "e"], ["d"]]


def test_refresh_never_readies_a_task_before_its_dependencies_succeed():
    graph = TaskGraph([T("a"), T("b", ["a"]), T("c", ["b"])])
    graph.refresh()
    assert [t.id for t in graph.ready()] == ["a"]
    graph.get("a").status = TaskStatus.RUNNING
    graph.refresh()
    assert graph.get("b").status is TaskStatus.PENDING
    graph.get("a").status = TaskStatus.SUCCESS
    graph.refresh()
    assert graph.get("b").status is TaskStatus.READY and graph.get("c").status is TaskStatus.PENDING


def test_failure_blocks_dependents_transitively_but_optional_does_not():
    graph = TaskGraph([T("a"), T("b", ["a"]), T("c", ["b"]), T("opt", metadata={"optional": True}), T("d", ["opt"])])
    graph.get("a").status = TaskStatus.FAILED
    graph.get("opt").status = TaskStatus.FAILED
    graph.refresh()
    assert graph.get("b").status is TaskStatus.BLOCKED and graph.get("c").status is TaskStatus.BLOCKED
    assert graph.get("d").status is TaskStatus.READY


def test_priority_orders_ready_tasks():
    graph = TaskGraph([T("low", priority=9), T("high", priority=1), T("mid", priority=5)])
    graph.refresh()
    assert [t.id for t in graph.ready()] == ["high", "mid", "low"]


def test_output_references_resolve_with_types_and_guard_rails():
    graph = TaskGraph([
        T("a"),
        T("b", ["a"], input={"rows": "${a.count}", "text": "found ${a.count} in ${a.files.0}", "keep": 3}),
        T("c", input={"x": "${a.count}"}),
    ])  # fmt: skip
    a = graph.get("a")
    a.status, a.output = TaskStatus.SUCCESS, {"count": 7, "files": ["x.csv"]}
    assert graph.resolve_input(graph.get("b")) == {"rows": 7, "text": "found 7 in x.csv", "keep": 3}
    with pytest.raises(GraphError, match="without depending"):
        graph.resolve_input(graph.get("c"))
    graph.get("b").input = {"x": "${a.nope}"}
    with pytest.raises(GraphError, match="no field"):
        graph.resolve_input(graph.get("b"))


def test_round_trip_and_resume_reset():
    graph = TaskGraph([T("a"), T("b", ["a"])])
    graph.get("a").status = TaskStatus.SUCCESS
    graph.get("a").output = {"v": 1}
    graph.get("b").status = TaskStatus.FAILED
    graph.get("b").retry_count = 2
    clone = TaskGraph.from_rows(graph.to_list())
    assert clone.get("a").output == {"v": 1}
    reset = clone.reset_for_resume()
    assert [t.id for t in reset] == ["b"] and clone.get("b").retry_count == 0
    assert clone.get("a").status is TaskStatus.SUCCESS


# ------------------------------------------------------------------- executor
async def test_independent_tasks_run_in_parallel_and_respect_the_limit():
    graph = TaskGraph([T(f"t{i}") for i in range(8)])

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        await asyncio.sleep(0.15)
        return TaskOutcome({"ok": True})

    executor = ParallelExecutor(handler, max_concurrency=4, healing=FAST)
    started = time.perf_counter()
    counts = await executor.run(graph)
    elapsed = time.perf_counter() - started
    assert counts == {"SUCCESS": 8}
    assert executor.peak_concurrency == 4
    assert elapsed < 0.15 * 8 * 0.6, f"ran serially? {elapsed:.2f}s"


async def test_dependents_only_start_after_dependencies_succeed():
    order: list[str] = []
    graph = TaskGraph([T("a"), T("b"), T("c", ["a", "b"], input={"sum": "${a.v}"}), T("d", ["c"])])

    async def handler(task: Task, inp: dict) -> TaskOutcome:
        order.append(f"start:{task.id}")
        await asyncio.sleep(0.03 if task.id == "a" else 0.01)
        order.append(f"end:{task.id}")
        return TaskOutcome({"v": 1, "saw": inp})

    await ParallelExecutor(handler, healing=FAST).run(graph)
    assert order.index("start:c") > order.index("end:a") and order.index("start:c") > order.index("end:b")
    assert order.index("start:d") > order.index("end:c")
    assert graph.get("c").output["saw"] == {"sum": 1}


async def test_failed_task_blocks_dependents_and_spares_independent_work():
    graph = TaskGraph([T("bad", max_retries=0), T("child", ["bad"]), T("other")])

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        if task.id == "bad":
            raise DependencyMissing("ffmpeg missing", hint="install it")
        return TaskOutcome({})

    counts = await ParallelExecutor(handler, healing=FAST).run(graph)
    assert counts == {"FAILED": 1, "BLOCKED": 1, "SUCCESS": 1}
    assert graph.get("bad").metadata["failure"]["remediation"] == "install it"
    assert "blocked" in graph.get("child").error


async def test_transient_failure_is_retried_with_a_modified_strategy():
    seen: list[dict] = []
    graph = TaskGraph([T("flaky", max_retries=3)])

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        seen.append(dict(task.metadata.get("recovery") or {}))
        if len(seen) == 1:
            raise ProviderError("p1", ProviderStatus.RATE_LIMITED, "429", retry_after=120)
        if len(seen) == 2:
            raise ValidationFailed("field 'title' missing")
        return TaskOutcome({"done": True})

    await ParallelExecutor(handler, healing=FAST).run(graph)
    task = graph.get("flaky")
    assert task.status is TaskStatus.SUCCESS and task.retry_count == 2
    assert seen[0] == {}
    assert seen[1]["exclude_providers"] == ["p1"]  # switched provider instead of repeating
    assert "title" in seen[2]["repair_hint"]  # and carried the repair hint forward


async def test_timeout_is_enforced_and_recovery_extends_it():
    calls: list[float] = []
    graph = TaskGraph([T("slow", timeout=0.05, max_retries=1)])

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        calls.append(time.perf_counter())
        await asyncio.sleep(0.08)
        return TaskOutcome({})

    await ParallelExecutor(handler, healing=FAST).run(graph)
    task = graph.get("slow")
    assert len(calls) == 2  # first attempt timed out at 0.05s, second ran with a 2x timeout
    assert task.status is TaskStatus.SUCCESS and task.metadata["recovery"]["timeout_multiplier"] == 2.0


async def test_cancellation_stops_running_and_pending_tasks():
    graph = TaskGraph([T("long"), T("after", ["long"])])
    cancel = asyncio.Event()

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        await asyncio.sleep(5)
        return TaskOutcome({})

    runner = asyncio.create_task(ParallelExecutor(handler, healing=FAST).run(graph, cancel))
    await asyncio.sleep(0.05)
    cancel.set()
    counts = await asyncio.wait_for(runner, timeout=2)
    assert counts == {"CANCELLED": 2}


async def test_approval_parks_the_task_without_failing_the_run():
    graph = TaskGraph([T("publish"), T("after", ["publish"]), T("free")])

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        if task.id == "publish":
            raise ApprovalRequired("apr_1", "social.publish", "external publishing")
        return TaskOutcome({})

    counts = await ParallelExecutor(handler, healing=FAST).run(graph)
    assert counts == {"WAITING_APPROVAL": 1, "PENDING": 1, "SUCCESS": 1}
    assert graph.get("publish").metadata["approval_id"] == "apr_1"


async def test_tasks_can_spawn_followup_tasks_at_runtime():
    graph = TaskGraph([T("plan")])

    async def handler(task: Task, inp: dict) -> TaskOutcome:
        if task.id == "plan":
            scenes = [T(f"scene{i}", ["plan"]) for i in range(3)]
            join = T("render", ["scene0", "scene1", "scene2"], input={"clips": ["${scene0.clip}", "${scene2.clip}"]})
            return TaskOutcome({"scenes": 3}, spawned=[*scenes, join])
        if task.id == "render":
            return TaskOutcome({"clips": inp["clips"]})
        return TaskOutcome({"clip": f"{task.id}.mp4"})

    counts = await ParallelExecutor(handler, healing=FAST).run(graph)
    assert counts == {"SUCCESS": 5}
    assert graph.get("render").output == {"clips": ["scene0.mp4", "scene2.mp4"]}


async def test_listener_sees_the_lifecycle_and_its_errors_do_not_break_execution():
    events: list[tuple[str, str]] = []
    graph = TaskGraph([T("a")])

    async def listener(task: Task, event: str, _data: dict) -> None:
        events.append((task.id, event))
        if event == "task.started":
            raise RuntimeError("telemetry down")

    async def handler(task: Task, _inp: dict) -> TaskOutcome:
        return TaskOutcome({})

    counts = await ParallelExecutor(handler, healing=FAST, listener=listener).run(graph)
    assert counts == {"SUCCESS": 1}
    assert [e for _, e in events] == ["task.ready", "task.started", "task.succeeded"]


# -------------------------------------------------------------------- healing
def test_healing_never_retries_final_decisions_or_missing_dependencies():
    engine = SelfHealingEngine()
    for error in (PathEscapeError("outside"), DependencyMissing("no ffmpeg"), FileNotFoundError("x.csv")):
        plan = engine.plan(T("t"), error)
        assert plan.action is RecoveryAction.FAIL and plan.remediation


def test_healing_picks_a_different_strategy_per_error_class():
    engine = SelfHealingEngine(base_delay=1)
    assert engine.plan(T("t"), TimeoutError()).modifications["timeout_multiplier"] == 2.0
    net = engine.plan(T("t"), ConnectionError("reset"))
    assert net.action is RecoveryAction.WAIT_RETRY and net.delay == 1
    auth = engine.plan(T("t"), ProviderError("openai", ProviderStatus.AUTH_ERROR, "401"))
    assert auth.action is RecoveryAction.SWITCH_PROVIDER and auth.modifications["exclude_providers"] == ["openai"]
    short = engine.plan(T("t"), ProviderError("x", ProviderStatus.RATE_LIMITED, "429", retry_after=3))
    assert short.action is RecoveryAction.WAIT_RETRY and short.delay == 3
    media = engine.plan(T("t"), MediaError("black frames"))
    assert media.error_class is ErrorClass.MEDIA and media.modifications["seed_bump"] == 1


def test_healing_refuses_to_repeat_an_identical_attempt():
    engine = SelfHealingEngine()
    task = T("t", max_retries=5)
    first = engine.plan(task, ValidationFailed("same error"))
    assert first.retries
    task.retry_count += 1
    task.metadata["recovery"] = first.modifications
    second = engine.plan(task, ValidationFailed("same error"))
    assert second.action is RecoveryAction.FAIL and "already tried" in second.reason


def test_healing_stops_when_the_retry_budget_is_spent():
    task = T("t", max_retries=1)
    task.retry_count = 1
    plan = SelfHealingEngine().plan(task, ConnectionError("down"))
    assert plan.action is RecoveryAction.FAIL and "exhausted" in plan.reason
