"""Price-action analytics on OHLCV data: structure, BOS/CHoCH, FVG, order blocks, liquidity,
CRT/TBS sweeps, EMAs, volume and session-candle breakouts.

Everything here is deterministic arithmetic on candles the user supplies. It describes
what the chart shows; it does not predict, and nothing in this package can place a trade.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..data import table as tables
from ..errors import ValidationFailed

_TIME_NAMES = ("time", "datetime", "date", "timestamp", "gmt time", "local time")
_FIELD_NAMES = {
    "open": ("open", "o"), "high": ("high", "h"), "low": ("low", "l"), "close": ("close", "c", "last"),
    "volume": ("volume", "vol", "v", "tick volume", "tick_volume"),
}  # fmt: skip
_TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y.%m.%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                 "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d-%m-%Y %H:%M")  # fmt: skip


@dataclass(frozen=True)
class Candle:
    time: datetime | None
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def body_low(self) -> float:
        return min(self.open, self.close)

    @property
    def body_high(self) -> float:
        return max(self.open, self.close)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if value is None:
        return None
    number = tables.parse_number(value)
    if number is not None and number > 1e8:  # epoch seconds or milliseconds
        return datetime.fromtimestamp(number / 1000 if number > 1e11 else number, UTC)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def load_candles(path: Path, sheet: str | None = None) -> list[Candle]:
    table = tables.load_table(path, sheet=sheet)
    lookup = {column.strip().casefold(): column for column in table.columns}

    def find(names: tuple[str, ...], required: bool = True) -> str | None:
        for name in names:
            if name in lookup:
                return lookup[name]
        if required:
            raise ValidationFailed(f"no '{names[0]}' column in {path.name}", hint="columns: " + ", ".join(table.columns))
        return None

    columns = {field: find(names, required=field != "volume") for field, names in _FIELD_NAMES.items()}
    time_column = find(_TIME_NAMES, required=False)
    candles: list[Candle] = []
    for row in table.rows:
        values = {f: tables.parse_number(row.get(c)) if c else 0.0 for f, c in columns.items()}
        o, h, low, c = (values[f] for f in ("open", "high", "low", "close"))
        if o is None or h is None or low is None or c is None:
            continue
        candles.append(Candle(_parse_time(row.get(time_column)) if time_column else None, o, h, low, c, float(values["volume"] or 0.0)))
    if len(candles) < 10:
        raise ValidationFailed(f"need at least 10 valid candles, found {len(candles)} in {path.name}")
    if all(c.time for c in candles):
        candles.sort(key=lambda c: c.time)  # type: ignore[arg-type,return-value]
    return candles


# ------------------------------------------------------------------ indicators
def ema(values: list[float], period: int) -> list[float | None]:
    if period <= 0 or len(values) < period:
        return [None] * len(values)
    out: list[float | None] = [None] * (period - 1)
    current = sum(values[:period]) / period
    out.append(current)
    alpha = 2 / (period + 1)
    for value in values[period:]:
        current = value * alpha + current * (1 - alpha)
        out.append(current)
    return out


def atr(candles: list[Candle], period: int = 14) -> float:
    ranges = [
        max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close)) for p, c in zip(candles, candles[1:], strict=False)
    ]
    tail = ranges[-period:] or [candles[-1].high - candles[-1].low]
    return sum(tail) / len(tail)


def timeframe_minutes(candles: list[Candle]) -> float | None:
    times = [c.time for c in candles if c.time]
    if len(times) < 3:
        return None
    deltas = [(b - a).total_seconds() / 60 for a, b in zip(times, times[1:], strict=False) if b > a]
    return statistics.median(deltas) if deltas else None


# ------------------------------------------------------------------- structure
def swings(candles: list[Candle], left: int = 2, right: int = 2) -> list[dict[str, Any]]:
    found = []
    for i in range(left, len(candles) - right):
        window = candles[i - left : i + right + 1]
        if candles[i].high == max(c.high for c in window) and sum(1 for c in window if c.high == candles[i].high) == 1:
            found.append({"index": i, "kind": "high", "price": candles[i].high})
        if candles[i].low == min(c.low for c in window) and sum(1 for c in window if c.low == candles[i].low) == 1:
            found.append({"index": i, "kind": "low", "price": candles[i].low})
    return found


def structure(candles: list[Candle], points: list[dict[str, Any]], right: int = 2) -> dict[str, Any]:
    """Label swings (HH/HL/LH/LL) and list breaks of structure and changes of character.

    A swing only becomes usable ``right`` candles after it forms, so no event uses information
    from its own future.
    """
    last_high = last_low = None
    for point in points:
        if point["kind"] == "high":
            point["label"] = "HH" if last_high is None or point["price"] > last_high else "LH"
            last_high = point["price"]
        else:
            point["label"] = "HL" if last_low is None or point["price"] > last_low else "LL"
            last_low = point["price"]

    events: list[dict[str, Any]] = []
    trend = "range"
    active_high = active_low = None
    queue = sorted(points, key=lambda p: p["index"])
    cursor = 0
    for i, candle in enumerate(candles):
        while cursor < len(queue) and queue[cursor]["index"] + right <= i:
            point = queue[cursor]
            if point["kind"] == "high":
                active_high = point
            else:
                active_low = point
            cursor += 1
        if active_high and candle.close > active_high["price"]:
            kind = "CHoCH" if trend == "down" else "BOS"
            events.append({"index": i, "type": kind, "direction": "bullish", "level": active_high["price"], "swing_index": active_high["index"]})
            trend, active_high = "up", None
        elif active_low and candle.close < active_low["price"]:
            kind = "CHoCH" if trend == "up" else "BOS"
            events.append({"index": i, "type": kind, "direction": "bearish", "level": active_low["price"], "swing_index": active_low["index"]})
            trend, active_low = "down", None
    return {"trend": trend, "events": events, "swings": points}


def fair_value_gaps(candles: list[Candle], min_size: float = 0.0) -> list[dict[str, Any]]:
    gaps = []
    for i in range(1, len(candles) - 1):
        before, after = candles[i - 1], candles[i + 1]
        if after.low > before.high and after.low - before.high > min_size:
            zone: dict[str, Any] = {"index": i, "direction": "bullish", "low": before.high, "high": after.low}
        elif after.high < before.low and before.low - after.high > min_size:
            zone = {"index": i, "direction": "bearish", "low": after.high, "high": before.low}
        else:
            continue
        later = candles[i + 2 :]
        if zone["direction"] == "bullish":
            zone["status"] = "filled" if any(c.low <= zone["low"] for c in later) else ("tapped" if any(c.low <= zone["high"] for c in later) else "open")
        else:
            zone["status"] = "filled" if any(c.high >= zone["high"] for c in later) else ("tapped" if any(c.high >= zone["low"] for c in later) else "open")
        gaps.append(zone)
    return gaps


def order_blocks(candles: list[Candle], events: list[dict[str, Any]], lookback: int = 12) -> list[dict[str, Any]]:
    """The last opposite-coloured candle before the move that broke structure."""
    blocks = []
    for event in events:
        want_bearish = event["direction"] == "bullish"
        start = max(0, event["index"] - lookback)
        for i in range(event["index"] - 1, start - 1, -1):
            candle = candles[i]
            if (want_bearish and not candle.bullish and candle.close != candle.open) or (not want_bearish and candle.bullish):
                later = candles[event["index"] + 1 :]
                touched = any(c.low <= candle.high and c.high >= candle.low for c in later)
                blocks.append({"index": i, "direction": event["direction"], "low": candle.low, "high": candle.high,
                               "from_event": event["type"], "status": "mitigated" if touched else "fresh"})  # fmt: skip
                break
    return blocks


def liquidity(candles: list[Candle], points: list[dict[str, Any]], tolerance: float) -> dict[str, Any]:
    """Equal highs/lows (resting liquidity) and the candles that swept them."""
    pools = []
    for kind in ("high", "low"):
        prices = sorted((p for p in points if p["kind"] == kind), key=lambda p: p["price"])
        cluster: list[dict[str, Any]] = []
        for point in prices + [{"price": float("inf"), "index": -1}]:
            if cluster and point["price"] - cluster[-1]["price"] > tolerance:
                if len(cluster) >= 2:
                    level = max(p["price"] for p in cluster) if kind == "high" else min(p["price"] for p in cluster)
                    pools.append({"side": "buy-side" if kind == "high" else "sell-side", "level": level,
                                  "touches": len(cluster), "last_index": max(p["index"] for p in cluster)})  # fmt: skip
                cluster = []
            cluster.append(point)
    sweeps = []
    for pool in pools:
        for i in range(pool["last_index"] + 1, len(candles)):
            candle = candles[i]
            if pool["side"] == "buy-side" and candle.high > pool["level"]:
                pool["swept_at"] = i
                sweeps.append({"index": i, "side": "buy-side", "level": pool["level"], "rejected": candle.close < pool["level"]})
                break
            if pool["side"] == "sell-side" and candle.low < pool["level"]:
                pool["swept_at"] = i
                sweeps.append({"index": i, "side": "sell-side", "level": pool["level"], "rejected": candle.close > pool["level"]})
                break
    return {"pools": pools, "sweeps": sweeps}


def crt_sweeps(candles: list[Candle]) -> list[dict[str, Any]]:
    """Candle Range Theory: a candle takes the previous candle's high/low and closes back inside.

    ``tbs`` marks the stricter case where only the wick left the range (the whole body stayed inside).
    """
    found = []
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        if cur.high > prev.high and cur.close < prev.high and cur.low >= prev.low:
            found.append({"index": i, "direction": "bearish", "swept": prev.high, "range": [prev.low, prev.high],
                          "tbs": cur.body_high <= prev.high and cur.body_low >= prev.low})  # fmt: skip
        elif cur.low < prev.low and cur.close > prev.low and cur.high <= prev.high:
            found.append({"index": i, "direction": "bullish", "swept": prev.low, "range": [prev.low, prev.high],
                          "tbs": cur.body_high <= prev.high and cur.body_low >= prev.low})  # fmt: skip
    return found


def volume_spikes(candles: list[Candle], window: int = 20, factor: float = 1.8) -> list[dict[str, Any]]:
    spikes = []
    for i in range(window, len(candles)):
        average = sum(c.volume for c in candles[i - window : i]) / window
        if average > 0 and candles[i].volume >= average * factor:
            spikes.append({"index": i, "ratio": round(candles[i].volume / average, 2), "bullish": candles[i].bullish})
    return spikes


def session_breakouts(candles: list[Candle], hour: int, minute: int) -> list[dict[str, Any]]:
    """For each day, the candle opening at hour:minute UTC and the first later close outside its range."""
    results = []
    for i, candle in enumerate(candles):
        if candle.time is None or (candle.time.hour, candle.time.minute) != (hour, minute):
            continue
        entry = {"index": i, "date": candle.time.date().isoformat(), "high": candle.high, "low": candle.low,
                 "direction": "none", "break_index": None}  # fmt: skip
        for j in range(i + 1, len(candles)):
            later = candles[j]
            if later.time is None or later.time.date() != candle.time.date():
                break
            if later.close > candle.high or later.close < candle.low:
                entry.update(direction="up" if later.close > candle.high else "down", break_index=j)
                break
        results.append(entry)
    return results


# --------------------------------------------------------------------- summary
def _stamp(candles: list[Candle], index: int | None) -> str | None:
    if index is None or not (0 <= index < len(candles)):
        return None
    moment = candles[index].time
    return moment.strftime("%Y-%m-%d %H:%M UTC") if moment is not None else None


def analyze(candles: list[Candle], *, sessions: list[tuple[int, int]] | None = None) -> dict[str, Any]:
    closes = [c.close for c in candles]
    last = candles[-1]
    average_range = atr(candles)
    points = swings(candles)
    market = structure(candles, points)
    gaps = fair_value_gaps(candles, min_size=average_range * 0.1)
    blocks = order_blocks(candles, market["events"])
    pools = liquidity(candles, points, tolerance=average_range * 0.15)
    emas = {period: ema(closes, period)[-1] for period in (20, 50, 200)}

    ema_bias = "neutral"
    if emas[20] and emas[50]:
        if last.close > emas[20] > emas[50]:
            ema_bias = "bullish"
        elif last.close < emas[20] < emas[50]:
            ema_bias = "bearish"
    structure_bias = {"up": "bullish", "down": "bearish"}.get(market["trend"], "neutral")
    bias = structure_bias if structure_bias == ema_bias else ("mixed" if "neutral" not in (structure_bias, ema_bias) else structure_bias if structure_bias != "neutral" else ema_bias)

    highs = [p for p in points if p["kind"] == "high"]
    lows = [p for p in points if p["kind"] == "low"]
    def annotate(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**item, "time": _stamp(candles, item.get("index"))} for item in items]

    scenarios = []
    if highs and lows:
        support, resistance = lows[-1]["price"], highs[-1]["price"]
        scenarios = [
            {"name": "bullish continuation", "condition": f"price holds above {support:g} and closes above {resistance:g}",
             "invalidation": f"a close below {support:g}"},
            {"name": "bearish continuation", "condition": f"price stays below {resistance:g} and closes below {support:g}",
             "invalidation": f"a close above {resistance:g}"},
        ]  # fmt: skip
    report: dict[str, Any] = {
        "candles": len(candles),
        "timeframe_minutes": timeframe_minutes(candles),
        "from": _stamp(candles, 0),
        "to": _stamp(candles, len(candles) - 1),
        "last_close": last.close,
        "atr14": round(average_range, 6),
        "bias": bias,
        "structure": {"trend": market["trend"], "bias": structure_bias, "events": annotate(market["events"][-8:]),
                      "recent_swings": annotate(points[-8:])},
        "ema": {"ema20": emas[20], "ema50": emas[50], "ema200": emas[200], "bias": ema_bias},
        "fair_value_gaps": annotate([g for g in gaps if g["status"] != "filled"][-6:]),
        "order_blocks": annotate([b for b in blocks if b["status"] == "fresh"][-5:]),
        "liquidity": {"pools": [p for p in pools["pools"] if "swept_at" not in p][-6:], "sweeps": annotate(pools["sweeps"][-5:])},
        "crt_sweeps": annotate(crt_sweeps(candles)[-5:]),
        "volume_spikes": annotate(volume_spikes(candles)[-5:]) if any(c.volume for c in candles) else [],
        "scenarios": scenarios,
        "disclaimer": "Descriptive analysis of the supplied candles. Not financial advice; no orders are placed.",
    }  # fmt: skip
    if sessions and all(c.time for c in candles):
        report["session_candles"] = {
            f"{hour:02d}:{minute:02d} UTC": annotate(session_breakouts(candles, hour, minute)[-5:]) for hour, minute in sessions
        }
    return report


# ------------------------------------------------------------------------ risk
def position_size(*, account: float, risk_percent: float, entry: float, stop: float, target: float | None = None,
                  contract_size: float = 1.0) -> dict[str, Any]:  # fmt: skip
    distance = abs(entry - stop)
    if distance <= 0:
        raise ValidationFailed("entry and stop must differ")
    if account <= 0 or not (0 < risk_percent <= 100):
        raise ValidationFailed("account must be positive and risk_percent between 0 and 100")
    risk_amount = account * risk_percent / 100
    units = risk_amount / (distance * contract_size)
    result: dict[str, Any] = {
        "direction": "long" if entry > stop else "short", "risk_amount": round(risk_amount, 2),
        "stop_distance": round(distance, 6), "position_size": round(units, 4), "loss_if_stopped": round(risk_amount, 2),
    }  # fmt: skip
    if target is not None:
        reward = abs(target - entry)
        on_right_side = (target > entry) == (entry > stop)
        result.update(reward_distance=round(reward, 6), risk_reward=round(reward / distance, 2),
                      profit_if_target=round(reward * units * contract_size, 2), target_on_profit_side=on_right_side)  # fmt: skip
    return result


# -------------------------------------------------------------------- backtest
def _stats(results: list[float], unit: str) -> dict[str, Any]:
    if not results:
        return {"trades": 0, "unit": unit, "note": "the rules produced no trades on this data"}
    wins = [r for r in results if r > 0]
    losses = [r for r in results if r <= 0]
    equity = peak = drawdown = 0.0
    for value in results:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(results), "unit": unit, "win_rate": round(len(wins) * 100 / len(results), 1),
        "total": round(sum(results), 4), "expectancy": round(sum(results) / len(results), 4),
        "average_win": round(sum(wins) / len(wins), 4) if wins else 0.0,
        "average_loss": round(sum(losses) / len(losses), 4) if losses else 0.0,
        "profit_factor": round(sum(wins) / gross_loss, 2) if gross_loss else None,
        "max_drawdown": round(drawdown, 4),
        "caveat": "simplified: no spread, commission or slippage; past results do not predict future results",
    }  # fmt: skip


def backtest_ema_cross(candles: list[Candle], fast: int = 20, slow: int = 50) -> dict[str, Any]:
    closes = [c.close for c in candles]
    fast_line, slow_line = ema(closes, fast), ema(closes, slow)
    position = 0
    entry = 0.0
    results: list[float] = []
    for i in range(1, len(candles)):
        f0, s0, f1, s1 = fast_line[i - 1], slow_line[i - 1], fast_line[i], slow_line[i]
        if None in (f0, s0, f1, s1):
            continue
        signal = 1 if f0 <= s0 and f1 > s1 else (-1 if f0 >= s0 and f1 < s1 else 0)  # type: ignore[operator]
        if signal and signal != position:
            if position:
                results.append((closes[i] - entry) / entry * 100 * position)
            position, entry = signal, closes[i]
    if position:
        results.append((closes[-1] - entry) / entry * 100 * position)
    return {"strategy": f"ema_cross({fast},{slow})", **_stats(results, "% per trade")}


def backtest_session_breakout(candles: list[Candle], hour: int, minute: int, rr: float = 2.0) -> dict[str, Any]:
    results: list[float] = []
    for setup in session_breakouts(candles, hour, minute):
        if setup["break_index"] is None:
            continue
        size = setup["high"] - setup["low"]
        if size <= 0:
            continue
        long = setup["direction"] == "up"
        entry = candles[setup["break_index"]].close
        stop = setup["low"] if long else setup["high"]
        risk = abs(entry - stop)
        if risk <= 0:
            continue
        target = entry + rr * risk if long else entry - rr * risk
        day = candles[setup["index"]].time.date()
        outcome = None
        last_close = entry
        for candle in candles[setup["break_index"] + 1 :]:
            if candle.time is None or candle.time.date() != day:
                break
            last_close = candle.close
            hit_stop = candle.low <= stop if long else candle.high >= stop
            hit_target = candle.high >= target if long else candle.low <= target
            if hit_stop:  # when both are touched in one candle, assume the stop came first
                outcome = -1.0
                break
            if hit_target:
                outcome = rr
                break
        if outcome is None:
            outcome = ((last_close - entry) if long else (entry - last_close)) / risk
        results.append(outcome)
    return {"strategy": f"session_breakout({hour:02d}:{minute:02d} UTC, rr={rr:g})", **_stats(results, "R per trade")}
