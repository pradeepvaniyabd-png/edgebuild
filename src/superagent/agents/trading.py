"""TradingAgent: chart analysis, position sizing, simple backtests and a trade journal.

Analysis only. There is no broker integration and no action that can place an order.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..trading import analysis as ta
from ..types import PermissionLevel
from .base import AgentContext, AgentResult, BaseAgent, action

_JOURNAL_COLUMNS = ["logged_at", "symbol", "direction", "setup", "entry", "stop", "target", "result_r", "notes"]


class ChartInput(BaseModel):
    path: str
    sheet: str | None = None
    symbol: str = ""
    dxy_path: str | None = None
    sessions: list[str] = Field(default_factory=lambda: ["14:30", "15:15"])

    @field_validator("sessions")
    @classmethod
    def _valid_sessions(cls, value: list[str]) -> list[str]:
        for item in value:
            hour, _, minute = item.partition(":")
            if not (hour.isdigit() and minute.isdigit() and 0 <= int(hour) < 24 and 0 <= int(minute) < 60):
                raise ValueError(f"session '{item}' must be HH:MM in UTC")
        return value


class RiskInput(BaseModel):
    account: float = Field(gt=0)
    risk_percent: float = Field(gt=0, le=100)
    entry: float
    stop: float
    target: float | None = None
    contract_size: float = Field(1.0, gt=0)


class BacktestInput(BaseModel):
    path: str
    sheet: str | None = None
    strategy: Literal["ema_cross", "session_breakout"] = "session_breakout"
    fast: int = Field(20, ge=2, le=400)
    slow: int = Field(50, ge=3, le=800)
    session: str = "14:30"
    rr: float = Field(2.0, gt=0, le=20)


class JournalInput(BaseModel):
    symbol: str = Field(min_length=1)
    direction: Literal["long", "short"]
    entry: float
    stop: float
    target: float | None = None
    result_r: float | None = None
    setup: str = ""
    notes: str = ""


def _session(text: str) -> tuple[int, int]:
    hour, _, minute = text.partition(":")
    return int(hour), int(minute)


class TradingAgent(BaseAgent):
    name = "trading"
    description = (
        "Reads OHLCV data and reports market structure (BOS/CHoCH), FVGs, order blocks, liquidity, CRT/TBS sweeps, "
        "EMA and DXY context, session-candle breakouts, position size and simple backtests. Never places trades."
    )
    capabilities = ["trading.analyze", "trading.risk", "trading.backtest", "trading.journal"]
    permissions = PermissionLevel.SAFE_WRITE

    @action("analyze", description="Analyse a chart export (CSV/XLSX with open, high, low, close[, volume, time]).", input=ChartInput)
    async def analyze(self, ctx: AgentContext, args: ChartInput) -> AgentResult:
        sandbox = ctx.tools.context.sandbox
        candles = await asyncio.to_thread(ta.load_candles, sandbox.resolve(args.path), args.sheet)
        report = ta.analyze(candles, sessions=[_session(s) for s in args.sessions])
        report["symbol"] = args.symbol
        warnings: list[str] = []
        if not all(c.time for c in candles):
            warnings.append("no usable time column: session-candle analysis was skipped")
        if args.dxy_path:
            dxy = ta.analyze(await asyncio.to_thread(ta.load_candles, sandbox.resolve(args.dxy_path)))
            opposite = {"bullish": "bearish", "bearish": "bullish"}
            report["dxy"] = {
                "bias": dxy["bias"], "trend": dxy["structure"]["trend"],
                "confluence": dxy["bias"] in opposite and opposite[dxy["bias"]] == report["bias"],
                "note": "USD-quoted assets usually move against DXY; confluence means the two biases disagree in direction",
            }  # fmt: skip
        if ctx.has_llm:
            prompt = ctx.prompt(
                f"Chart facts (computed, do not alter):\n{report}\n\n"
                "Explain in plain language what the structure shows and what would confirm or invalidate each scenario. "
                "Do not give buy/sell instructions or predictions."
            )
            report["commentary"] = (await ctx.ai.complete(prompt, system=ctx.system_prompt, max_tokens=1500)).text.strip()
        events = report["structure"]["events"]
        latest = f"; last event {events[-1]['type']} {events[-1]['direction']}" if events else ""
        return AgentResult(
            output=report, warnings=warnings,
            summary=f"{args.symbol or 'chart'}: bias {report['bias']}, structure {report['structure']['trend']}{latest}",
            verify=[{"type": "schema", "required": ["bias", "structure", "disclaimer"]},
                    {"type": "row_count", "key": "candles", "min": 10}],
        )  # fmt: skip

    @action("risk", description="Position size and risk/reward from account size, risk %, entry, stop and target.", input=RiskInput)
    async def risk(self, ctx: AgentContext, args: RiskInput) -> AgentResult:
        result = ta.position_size(**args.model_dump())
        warnings = []
        if result.get("target_on_profit_side") is False:
            warnings.append("the target is on the losing side of the entry for this stop: check the numbers")
        if args.risk_percent > 2:
            warnings.append(f"risking {args.risk_percent:g}% of the account on one trade is above the common 1-2% guideline")
        ratio = f", R:R 1:{result['risk_reward']:g}" if "risk_reward" in result else ""
        return AgentResult(
            output=result, warnings=warnings,
            summary=f"{result['direction']} size {result['position_size']:g}, risk {result['risk_amount']:g}{ratio}",
            verify=[{"type": "schema", "required": ["position_size", "risk_amount"]}],
        )

    @action("backtest", description="Replay a simple rule set (EMA cross or session-candle breakout) over the data.", input=BacktestInput)
    async def backtest(self, ctx: AgentContext, args: BacktestInput) -> AgentResult:
        candles = await asyncio.to_thread(ta.load_candles, ctx.tools.context.sandbox.resolve(args.path), args.sheet)
        if args.strategy == "ema_cross":
            result = ta.backtest_ema_cross(candles, args.fast, args.slow)
        else:
            result = ta.backtest_session_breakout(candles, *_session(args.session), rr=args.rr)
        result["candles"] = len(candles)
        if result["trades"]:
            summary = f"{result['strategy']}: {result['trades']} trades, win rate {result['win_rate']}%, expectancy {result['expectancy']} {result['unit']}"
        else:
            summary = f"{result['strategy']}: no trades on this data"
        warnings = ["fewer than 30 trades: the statistics are not reliable"] if 0 < result["trades"] < 30 else []
        return AgentResult(output=result, summary=summary, warnings=warnings, verify=[{"type": "schema", "required": ["strategy"]}])

    @action("journal", description="Append a trade to the journal file in the workspace.", input=JournalInput)
    async def journal(self, ctx: AgentContext, args: JournalInput) -> AgentResult:
        path = ctx.tools.context.sandbox.resolve("trading/journal.csv", write=True)
        row = {"logged_at": datetime.now(UTC).isoformat(timespec="seconds"), **args.model_dump()}
        count = await asyncio.to_thread(_append_journal, path, row)
        return AgentResult(
            output={"path": str(path), "entries": count}, summary=f"journal entry {count} saved", artifacts=[str(path)],
            verify=[{"type": "file_exists", "key": "path"}],
        )  # fmt: skip


def _append_journal(path: Path, row: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig" if is_new else "utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_JOURNAL_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow({key: "" if row.get(key) is None else row[key] for key in _JOURNAL_COLUMNS})
    with open(path, encoding="utf-8-sig") as handle:
        return max(0, sum(1 for _ in handle) - 1)
