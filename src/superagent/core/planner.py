"""Planner: turns an intent into a validated task graph.

Rule plans always work offline. When a language model is available and the request is
non-trivial, it may propose a richer plan; that plan is validated against the agent
catalog and discarded in favour of the rule plan if anything in it is invalid.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from ..agents.base import AgentRegistry
from ..config import Settings
from ..observability import get_logger
from ..types import Intent, ProviderKind
from ..utils import truncate_middle
from .dag import GraphError, Task, TaskGraph
from .intent import IntentResult

log = get_logger("planner")

_SCHEMES = {"sambal": "sambal", "संबल": "sambal", "samagra": "samagra", "समग्र": "samagra", "pension": "pension",
            "पेंशन": "pension", "cm helpline": "cm_helpline", "हेल्पलाइन": "cm_helpline", "ekyc": "ekyc", "e-kyc": "ekyc",
            "केवाईसी": "ekyc"}  # fmt: skip
_LETTER_TYPES = [("smaran", "smaran_patra"), ("स्मरण", "smaran_patra"), ("aadesh", "aadesh"), ("आदेश", "aadesh"),
                 ("suchna", "suchna"), ("सूचना", "suchna"), ("prativedan", "prativedan"), ("प्रतिवेदन", "prativedan")]  # fmt: skip
_TABLE_SUFFIX = (".xlsx", ".xlsm", ".csv", ".tsv")
_DOC_SUFFIX = (".pdf", ".docx", ".txt", ".md", ".json")


class PlannedTask(BaseModel):
    ref: str = Field(min_length=1, max_length=40)
    agent: str
    action: str
    description: str = ""
    input: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


class LLMPlan(BaseModel):
    tasks: list[PlannedTask] = Field(min_length=1, max_length=16)


class Plan(BaseModel):
    tasks: list[Task]
    method: str = "rules"
    notes: list[str] = Field(default_factory=list)

    def graph(self) -> TaskGraph:
        return TaskGraph(self.tasks)


def _first(paths: list[str], suffixes: tuple[str, ...]) -> str | None:
    return next((p for p in paths if p.lower().endswith(suffixes)), None)


class Planner:
    def __init__(self, agents: AgentRegistry, settings: Settings, ai: Any = None, creative: Any = None):
        self.agents = agents
        self.settings = settings
        self.ai = ai
        self.creative = creative  # optional: object with plan(command, intent) -> list[Task] for visual projects

    # ------------------------------------------------------------------ public
    async def plan(self, command: str, intent: IntentResult) -> Plan:
        rules = self.rule_plan(command, intent)
        wants_llm = (
            self.ai is not None and self.settings.llm_planning and intent.complexity != "simple"
            and intent.primary not in {Intent.SYSTEM} and self.ai.has(ProviderKind.LLM)
        )  # fmt: skip
        if not wants_llm:
            return rules
        try:
            return await self._llm_plan(command, intent, fallback=rules)
        except Exception:  # planning help is optional; the rule plan is always valid
            log.info("LLM planning unavailable; using the rule plan", exc_info=True)
            rules.notes.append("language-model planning failed; used the built-in plan")
            return rules

    # ------------------------------------------------------------------- rules
    def _task(self, agent: str, action: str, description: str, **kw: Any) -> Task:
        metadata = dict(kw.pop("metadata", {}) or {})
        defaults = {"max_retries": self.settings.max_retries, "timeout": self.settings.task_timeout}
        return Task(description=description, agent=agent, action=action, input=kw.pop("input", {}),
                    metadata=metadata, **{**defaults, **kw})  # fmt: skip

    def rule_plan(self, command: str, intent: IntentResult) -> Plan:
        entities = intent.entities
        paths: list[str] = entities.get("paths", [])
        urls: list[str] = entities.get("urls", [])
        tasks: list[Task] = []
        notes: list[str] = []
        lowered = command.lower()

        def add(task: Task) -> Task:
            tasks.append(task)
            return task

        table_path, doc_path = _first(paths, _TABLE_SUFFIX), _first(paths, _DOC_SUFFIX)
        handled: set[Intent] = set()

        # ---- creative requests are planned by the creative engine -----------------
        creative_intents = {Intent.VIDEO, Intent.IMAGE, Intent.VOICE, Intent.AUDIO, Intent.MULTIMODAL}
        wants_factory = self.creative is not None and getattr(self.creative, "wants", lambda _c: False)(command)
        if wants_factory or intent.has(*creative_intents) or (intent.has(Intent.CONTENT) and intent.has(Intent.IMAGE)):
            if self.creative is None:
                notes.append("creative pipeline is not installed; visual requests were skipped")
            else:
                brief_source = ""
                data_task: Task | None = None
                if table_path and intent.has(Intent.DATA, Intent.REPORT):  # "analyse the sheet, then make a video of it"
                    data_task = add(self._task("analyst", "analyze", f"analyse {table_path}", input={"path": table_path}))
                    brief_source = f"${{{data_task.id}.text}}"
                    handled |= {Intent.DATA, Intent.REPORT}
                creative = self.creative.plan(command, intent, depends_on=[data_task.id] if data_task else [], source=brief_source)
                tasks.extend(creative)
                handled |= creative_intents | {Intent.CONTENT}

        # ---- per-intent plans ---------------------------------------------------
        if Intent.GOVERNMENT in intent.intents:
            scheme = next((value for key, value in _SCHEMES.items() if key in lowered), None)
            letter_type = next((value for key, value in _LETTER_TYPES if key in lowered), "patra")
            if table_path and ("goswara" in lowered or "गोसवारा" in command):
                add(self._task("government", "goswara", f"build goswara from {table_path}", input={"path": table_path}))
            elif table_path:
                add(self._task("government", "scheme_report", f"pendency report from {table_path}",
                               input={"path": table_path, "scheme": scheme or "generic"}))  # fmt: skip
            else:
                add(self._task("government", "draft_letter", f"draft {letter_type.replace('_', ' ')}",
                               input={"subject": truncate_middle(command.strip(), 200), "letter_type": letter_type}))  # fmt: skip
                notes.append("letter details beyond the request text are left as marked blanks to fill in")
            handled |= {Intent.GOVERNMENT, Intent.REPORT, Intent.DATA, Intent.FILE}

        if Intent.TRADING in intent.intents:
            if table_path:
                dxy = next((p for p in paths if "dxy" in p.lower() and p != table_path), None)
                chart = next((p for p in paths if p.lower().endswith(_TABLE_SUFFIX) and "dxy" not in p.lower()), table_path)
                inputs: dict[str, Any] = {"path": chart}
                if dxy:
                    inputs["dxy_path"] = dxy
                symbol = re.search(r"(?<![a-z])(xau\s?usd|btc\s?usd|eur\s?usd|gbp\s?usd|us30|nas100)(?![a-z])", lowered)
                if symbol:
                    inputs["symbol"] = symbol.group(1).replace(" ", "").upper()
                add(self._task("trading", "analyze", f"analyse chart {chart}", input=inputs))
            else:
                notes.append("trading analysis needs an OHLCV export (CSV/XLSX): attach one and ask again")
            handled |= {Intent.TRADING, Intent.DATA}

        if Intent.DATA in intent.intents and Intent.DATA not in handled:
            if table_path:
                analysis = add(self._task("analyst", "analyze", f"analyse {table_path}", input={"path": table_path, "question": command}))
                if intent.has(Intent.REPORT):
                    fmt = "docx" if re.search(r"\b(docx|word)\b", lowered) else "md"
                    add(self._task("analyst", "report", "write the report", dependencies=[analysis.id],
                                   input={"analysis": f"${{{analysis.id}}}", "format": fmt}))  # fmt: skip
            else:
                notes.append("data analysis needs a spreadsheet (.xlsx/.csv): attach one and ask again")
            handled |= {Intent.DATA, Intent.REPORT}

        if Intent.FILE in intent.intents and Intent.FILE not in handled:
            if doc_path or table_path:
                target = doc_path or table_path
                action = "extract" if re.search(r"\b(extract|text nikalo)\b", lowered) else "summarize"
                add(self._task("file", action, f"{action} {target}", input={"path": target}))
            else:
                notes.append("no file was named: mention the file name or path")
            handled |= {Intent.FILE}

        if Intent.RESEARCH in intent.intents:
            if urls and not intent.has(Intent.CODING):
                for url in urls[:4]:
                    add(self._task("research", "fetch", f"read {url}", input={"url": url}, max_retries=1))
            else:
                add(self._task("research", "gather", f"research: {truncate_middle(command, 120)}", input={"query": truncate_middle(command, 300)}))

        if Intent.DEBUGGING in intent.intents:
            repo = paths[0] if paths and not paths[0].endswith((".py", ".js", ".ts", ".log")) else "."
            if intent.has(Intent.CODING):
                inspect = add(self._task("coder", "inspect", "inspect the repository", input={"repo": repo}))
                tests = add(self._task("coder", "run_tests", "run the test suite", dependencies=[inspect.id], input={"repo": repo}))
                add(self._task("debugger", "diagnose", "diagnose failures", dependencies=[tests.id],
                               input={"log": f"${{{tests.id}.output_tail}}"}))  # fmt: skip
            else:
                log_path = next((p for p in paths if p.endswith(".log")), None)
                if log_path:
                    read = add(self._task("file", "extract", f"read {log_path}", input={"path": log_path}))
                    add(self._task("debugger", "diagnose", "diagnose the log", dependencies=[read.id], input={"log": f"${{{read.id}.text}}"}))
                else:
                    add(self._task("debugger", "diagnose", "diagnose the error text", input={"log": command}))
            handled.add(Intent.CODING)
        elif Intent.CODING in intent.intents:
            repo = paths[0] if paths and not paths[0].endswith((".py", ".js", ".ts")) else "."
            inspect = add(self._task("coder", "inspect", "inspect the repository", input={"repo": repo}))
            if re.search(r"\b(review)\b", lowered):
                add(self._task("coder", "review", "review the current changes", dependencies=[inspect.id], input={"repo": repo}))
            else:
                add(self._task("coder", "implement", truncate_middle(command, 120), dependencies=[inspect.id],
                               input={"repo": repo, "instruction": command, "files": [p for p in paths if p.endswith((".py", ".js", ".ts"))]}))  # fmt: skip

        if Intent.SYSTEM in intent.intents:
            add(self._task("system", "status", "report platform status"))

        if Intent.AUTOMATION in intent.intents:
            notes.append("to run this on a schedule use: super-agent schedule add --name <n> --cron '<expr>' --command '<command>'")

        if not tasks:
            add(self._task("master", "answer", "answer the request", input={"question": command}))

        # ---- final synthesis over leaf tasks -------------------------------------
        depended = {dep for task in tasks for dep in task.dependencies}
        leaves = [task for task in tasks if task.id not in depended]
        if len(tasks) > 1 and not (len(tasks) == 1 and tasks[0].agent == "master"):
            add(self._task("master", "synthesize", "combine the results", dependencies=[leaf.id for leaf in leaves],
                           input={"goal": command, "results": [f"${{{leaf.id}}}" for leaf in leaves]}, priority=9))  # fmt: skip
        return Plan(tasks=tasks, method="rules", notes=notes)

    # --------------------------------------------------------------------- LLM
    def catalog_text(self) -> str:
        lines: list[str] = []
        for agent_name in self.agents.names():
            agent = self.agents.get(agent_name)
            for name, spec in agent.actions.items():
                schema = spec.input_model.model_json_schema()
                required = set(schema.get("required", []))
                fields = ", ".join(
                    f"{key}{'*' if key in required else ''}:{_type_of(prop)}" for key, prop in schema.get("properties", {}).items()
                )
                lines.append(f"- {agent_name}.{name}: {spec.description} inputs: {fields or 'none'}")
        return "\n".join(lines)

    async def _llm_plan(self, command: str, intent: IntentResult, *, fallback: Plan) -> Plan:
        prompt = (
            "Plan the user's request as a small dependency graph of tasks using ONLY these agent actions "
            "(a * marks a required input):\n" + self.catalog_text() + "\n\n"
            f"User request: {command}\nDetected intents: {[i.value for i in intent.intents]}\n"
            f"Detected entities: {json.dumps(intent.entities, ensure_ascii=False)}\n\n"
            "Rules:\n- Give every task a short unique `ref`; list the refs it needs under `depends_on`.\n"
            "- Refer to an earlier task's output as ${ref.field} (or ${ref} for all of it); only for tasks in depends_on.\n"
            "- Tasks without dependencies run in parallel, so do not chain independent work.\n"
            "- Use file paths exactly as the user gave them. Never invent file names.\n"
            "- End with master.synthesize when there are several tasks.\n"
            'Return {"tasks": [{"ref","agent","action","description","input","depends_on"}]}.'
        )
        plan = await self.ai.complete_json(prompt, LLMPlan, max_tokens=4000, attempts=2)
        mapping = {planned.ref: Task(description=planned.description or f"{planned.agent}.{planned.action}", agent=planned.agent,
                                     action=planned.action, max_retries=self.settings.max_retries, timeout=self.settings.task_timeout)
                   for planned in plan.tasks}  # fmt: skip
        if len(mapping) != len(plan.tasks):
            raise GraphError("duplicate task refs in the proposed plan")
        for planned in plan.tasks:
            task = mapping[planned.ref]
            unknown = [ref for ref in planned.depends_on if ref not in mapping]
            if unknown:
                raise GraphError(f"task '{planned.ref}' depends on unknown refs {unknown}")
            task.dependencies = [mapping[ref].id for ref in planned.depends_on]
            raw = json.dumps(planned.input, ensure_ascii=False)
            for ref, other in mapping.items():
                raw = raw.replace("${" + ref + ".", "${" + other.id + ".").replace("${" + ref + "}", "${" + other.id + "}")
            task.input = json.loads(raw)
            agent = self.agents.get(planned.agent)  # raises ValidationFailed for an unknown agent
            if "${" not in raw:
                agent.validate_input(planned.action, task.input)
            elif planned.action not in agent.actions:
                agent.validate_input(planned.action, {})
        tasks = list(mapping.values())
        TaskGraph(tasks)  # validates cycles and unknown dependencies
        for task in tasks:  # a reference is only legal if it points at a declared dependency
            for ref in re.findall(r"\$\{([A-Za-z0-9_\-]+)", json.dumps(task.input)):
                if ref not in task.dependencies:
                    raise GraphError(f"task '{task.description}' references '{ref}' without depending on it")
        notes = [*fallback.notes, "plan proposed by the language model and validated against the agent catalog"]
        return Plan(tasks=tasks, method="llm", notes=notes)


def _type_of(prop: dict[str, Any]) -> str:
    if "type" in prop:
        return str(prop["type"])
    if "anyOf" in prop:
        return "|".join(str(p.get("type", "any")) for p in prop["anyOf"])
    return "any"
