"""MasterAgent (answering and final synthesis), ResearchAgent and BrowserAgent."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..core.context import summarize_value
from ..core.dag import Task
from ..data.documents import extractive_summary
from ..errors import DependencyMissing
from ..types import PermissionLevel
from ..utils import truncate_middle
from .base import AgentContext, AgentResult, BaseAgent, action


class AnswerInput(BaseModel):
    question: str = Field(min_length=1)


class SynthesizeInput(BaseModel):
    goal: str = ""
    results: list[Any] = Field(default_factory=list)


class MasterAgent(BaseAgent):
    name = "master"
    description = "Answers general questions and turns the outputs of other agents into one final result."
    capabilities = ["general.answer", "result.synthesize"]
    permissions = PermissionLevel.READ_ONLY

    @action("answer", description="Answer a general question or request in the user's language.", input=AnswerInput, needs_llm=True)
    async def answer(self, ctx: AgentContext, args: AnswerInput) -> AgentResult:
        response = await ctx.ai.complete(ctx.prompt(args.question), system=ctx.system_prompt, max_tokens=4096)
        return AgentResult(
            output={"text": response.text.strip(), "provider": response.provider, "model": response.model},
            summary=truncate_middle(response.text.strip(), 400),
            verify=[{"type": "non_empty", "key": "text", "min_chars": 2}],
        )

    @action("synthesize", description="Combine the results of earlier tasks into a short final answer.", input=SynthesizeInput)
    async def synthesize(self, ctx: AgentContext, args: SynthesizeInput) -> AgentResult:
        digest = "\n\n".join(f"[{index + 1}] {summarize_value(item, 1800)}" for index, item in enumerate(args.results))
        warnings: list[str] = []
        if ctx.has_llm and digest:
            prompt = ctx.prompt(
                f"Goal: {args.goal or ctx.command}\n\nResults from the specialist agents:\n{digest}\n\n"
                "Write the final answer for the user: lead with the outcome, then the key facts and file paths. "
                "Use only what the results contain. Keep it concise."
            )
            text = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=2000)).text.strip()
            method = "llm"
        else:
            text = digest or "No task produced a result."
            method = "digest"
            warnings.append("no language model configured: the final answer is a plain digest of task results")
        return AgentResult(
            output={"text": text, "method": method}, summary=truncate_middle(text, 400), warnings=warnings,
            verify=[{"type": "non_empty", "key": "text"}],
        )  # fmt: skip


# -------------------------------------------------------------------- research
class SearchInput(BaseModel):
    query: str = Field(min_length=1)
    max_results: int = Field(6, ge=1, le=20)


class FetchInput(BaseModel):
    url: str
    max_chars: int = Field(12_000, ge=500, le=100_000)


class GatherInput(BaseModel):
    query: str = Field(min_length=1)
    max_sources: int = Field(4, ge=1, le=8)


class ResearchSynthInput(BaseModel):
    question: str
    sources: list[Any] = Field(default_factory=list)


class ResearchAgent(BaseAgent):
    name = "research"
    description = "Finds sources on the web, reads them in parallel and reports what they say with links."
    capabilities = ["web.search", "web.read", "research.report"]
    permissions = PermissionLevel.READ_ONLY

    @action("search", description="Search the web for a query.", input=SearchInput)
    async def search(self, ctx: AgentContext, args: SearchInput) -> AgentResult:
        found = await ctx.tools.call("web.search", query=args.query, max_results=args.max_results)
        return AgentResult(output=found, summary=f"{len(found['results'])} results for '{args.query}'")

    @action("fetch", description="Read one web page and return its text.", input=FetchInput)
    async def fetch(self, ctx: AgentContext, args: FetchInput) -> AgentResult:
        page = await ctx.tools.call("web.fetch", url=args.url, max_chars=args.max_chars)
        return AgentResult(
            output={"url": page["url"], "title": page["title"], "text": page["text"], "status": page["status"]},
            summary=f"read {page['title'] or page['url']} ({len(page['text'])} chars)",
            verify=[{"type": "one_of", "key": "status", "values": [200, 203]}, {"type": "non_empty", "key": "text", "min_chars": 50}],
        )  # fmt: skip

    @action("gather", description="Search, then read the top sources in parallel and write a sourced report.", input=GatherInput)
    async def gather(self, ctx: AgentContext, args: GatherInput) -> AgentResult:
        found = await ctx.tools.call("web.search", query=args.query, max_results=args.max_sources * 2)
        urls: list[str] = []
        for result in found["results"]:
            if result["url"] not in urls:
                urls.append(result["url"])
        urls = urls[: args.max_sources]
        if not urls:
            raise DependencyMissing(f"the search for '{args.query}' returned no sources")
        readers = [
            Task(description=f"read {url}", agent="research", action="fetch", parent_id=ctx.task.id,
                 dependencies=[ctx.task.id], input={"url": url}, max_retries=1, timeout=120,
                 metadata={"optional": True})  # one dead link must not sink the report
            for url in urls
        ]  # fmt: skip
        report = Task(
            description=f"write a sourced report on: {args.query}", agent="research", action="synthesize",
            parent_id=ctx.task.id, dependencies=[reader.id for reader in readers],
            input={"question": args.query, "sources": [f"${{{reader.id}.url}}|||${{{reader.id}.text}}" for reader in readers]},
        )  # fmt: skip
        return AgentResult(
            output={"query": args.query, "urls": urls, "report_task": report.id},
            summary=f"reading {len(urls)} sources in parallel", spawn=[*readers, report],
        )  # fmt: skip

    @action("synthesize", description="Write a report from fetched sources, citing each one.", input=ResearchSynthInput)
    async def synthesize(self, ctx: AgentContext, args: ResearchSynthInput) -> AgentResult:
        sources = []
        for item in args.sources:
            if isinstance(item, dict):
                url, text = str(item.get("url", "")), str(item.get("text", ""))
            else:
                url, _, text = str(item or "").partition("|||")
            if text.strip():
                sources.append({"url": url, "text": text})
        if not sources:
            raise DependencyMissing("none of the sources could be read")
        warnings: list[str] = []
        if ctx.has_llm:
            material = "\n\n".join(f"[{i + 1}] {s['url']}\n{truncate_middle(s['text'], 6000)}" for i, s in enumerate(sources))
            prompt = ctx.prompt(
                f"Question: {args.question}\n\nSources:\n{material}\n\n"
                "Answer using only these sources. Cite as [n] after each claim. Say plainly where sources "
                "disagree or do not cover the question. End with a 'Sources' list of [n] URL."
            )
            text = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=3000)).text.strip()
            method = "llm"
        else:
            parts = [f"[{i + 1}] {s['url']}\n{extractive_summary(s['text'], 4)}" for i, s in enumerate(sources)]
            text = f"Extracts on: {args.question}\n\n" + "\n\n".join(parts)
            method = "extractive"
            warnings.append("no language model configured: this is an extract of each source, not a reasoned answer")
        return AgentResult(
            output={"text": text, "method": method, "sources": [s["url"] for s in sources]},
            summary=truncate_middle(text, 400), warnings=warnings,
            verify=[{"type": "non_empty", "key": "text", "min_chars": 40}, {"type": "list_min", "key": "sources", "min": 1}],
        )  # fmt: skip


class OpenInput(BaseModel):
    url: str


class BrowserAgent(BaseAgent):
    name = "browser"
    description = "Opens a web page and returns its readable text and links. Static pages only; it does not click, log in or submit forms."
    capabilities = ["web.read"]
    permissions = PermissionLevel.READ_ONLY

    @action("open", description="Open a URL and return the page title, text and links.", input=OpenInput)
    async def open(self, ctx: AgentContext, args: OpenInput) -> AgentResult:
        page = await ctx.tools.call("browser.open", url=args.url)
        return AgentResult(
            output={"url": page["url"], "title": page["title"], "text": page["text"], "links": page["links"][:40]},
            summary=f"opened {page['title'] or page['url']}",
            verify=[{"type": "non_empty", "key": "text"}],
        )
