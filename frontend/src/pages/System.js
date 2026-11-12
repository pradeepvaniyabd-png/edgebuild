import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from "react";
import { api, usePoll } from "../api";
import { Badge, Bar, Empty, ErrorNote, Page, money, when } from "../components/ui";
export function Providers() {
    const [probe, setProbe] = useState(false);
    const data = usePoll(`/api/v1/providers?probe=${probe}`, probe ? 0 : 10000);
    return (_jsxs(Page, { title: "Providers", subtitle: "Local first, then free, then paid cloud. Anything unconfigured shows what to set.", actions: _jsx("button", { className: "btn-ghost", onClick: () => setProbe(true), children: "Test connections" }), children: [_jsx("div", { className: "card overflow-x-auto p-0", children: _jsxs("table", { className: "w-full", children: [_jsx("thead", { children: _jsx("tr", { children: ["Kind", "Provider", "Tier", "Status", "Detail"].map((h) => _jsx("th", { className: "th", children: h }, h)) }) }), _jsx("tbody", { children: (data.data ?? []).map((p) => _jsxs("tr", { className: "border-t border-ink-100 dark:border-ink-800", children: [_jsx("td", { className: "td", children: p.kind }), _jsx("td", { className: "td font-medium", children: p.name }), _jsx("td", { className: "td text-ink-400", children: String(p.tier).replaceAll("_", " ") }), _jsx("td", { className: "td", children: _jsx(Badge, { status: p.status }) }), _jsxs("td", { className: "td text-ink-400", children: [p.detail, p.cooldown_seconds > 0 ? ` (cooling down ${Math.round(p.cooldown_seconds)}s)` : ""] })] }, p.name)) })] }) }), _jsx(ErrorNote, { message: data.error })] }));
}
export function Costs() {
    const costs = usePoll("/api/v1/costs", 8000);
    const b = costs.data?.budgets ?? {};
    return (_jsxs(Page, { title: "Costs", subtitle: "Local and free providers cost nothing. Paid calls over a budget wait for your approval.", children: [_jsxs("div", { className: "card space-y-3", children: [_jsxs("div", { children: [_jsxs("div", { className: "mb-1 flex justify-between text-sm", children: [_jsx("span", { children: "Today" }), _jsxs("span", { children: [money(costs.data?.today_usd), " / ", money(b.daily_usd)] })] }), _jsx(Bar, { value: costs.data?.today_usd ?? 0, max: b.daily_usd ?? 0 })] }), _jsxs("div", { className: "text-sm text-ink-400", children: ["Per project ", money(b.project_usd), " \u00B7 per generation ", money(b.per_generation_usd), " \u00B7 all time ", money(costs.data?.total_usd)] })] }), _jsxs("div", { className: "card overflow-x-auto p-0", children: [_jsxs("table", { className: "w-full", children: [_jsx("thead", { children: _jsx("tr", { children: ["Provider", "Calls", "Spend", "Unpriced calls"].map((h) => _jsx("th", { className: "th", children: h }, h)) }) }), _jsx("tbody", { children: Object.entries((costs.data?.by_provider ?? {})).map(([name, v]) => _jsxs("tr", { className: "border-t border-ink-100 dark:border-ink-800", children: [_jsx("td", { className: "td", children: name }), _jsx("td", { className: "td", children: v.calls }), _jsx("td", { className: "td", children: money(v.usd) }), _jsx("td", { className: "td", children: v.unpriced_calls })] }, name)) })] }), !Object.keys(costs.data?.by_provider ?? {}).length && _jsx("div", { className: "p-4", children: _jsx(Empty, { children: "No usage recorded yet." }) })] }), costs.data?.note && _jsx("p", { className: "text-xs text-ink-400", children: costs.data.note })] }));
}
export function Logs() {
    const logs = usePoll("/api/v1/logs?limit=300", 2500);
    const [level, setLevel] = useState("");
    const rows = (logs.data ?? []).filter((r) => !level || r.level === level).slice(-200);
    return (_jsx(Page, { title: "Live logs", subtitle: "Structured, secret-masked server log.", actions: _jsxs("select", { "aria-label": "Level", className: "input w-32", value: level, onChange: (e) => setLevel(e.target.value), children: [_jsx("option", { value: "", children: "All" }), _jsx("option", { children: "INFO" }), _jsx("option", { children: "WARNING" }), _jsx("option", { children: "ERROR" })] }), children: _jsxs("div", { className: "card max-h-[70vh] overflow-y-auto font-mono text-xs", children: [rows.map((r, i) => _jsxs("div", { className: r.level === "ERROR" ? "text-red-600" : r.level === "WARNING" ? "text-amber-600" : "", children: [_jsx("span", { className: "text-ink-400", children: String(r.ts).slice(11, 19) }), " ", r.level, " ", r.agent ? `[${r.agent}${r.tool ? "/" + r.tool : ""}] ` : "", r.msg] }, i)), !rows.length && _jsx("span", { className: "text-ink-400", children: "No log lines yet." })] }) }));
}
export function Approvals() {
    const list = usePoll("/api/v1/approvals?status=PENDING", 2500);
    const [error, setError] = useState("");
    const decide = async (a, approve) => {
        setError("");
        try {
            await api(`/api/v1/approvals/${a.id}`, { body: { approve } });
            list.reload();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };
    return (_jsxs(Page, { title: "Approvals", subtitle: "Risky or paid actions wait here. Nothing happens until you decide.", children: [_jsx(ErrorNote, { message: error }), (list.data ?? []).map((a) => (_jsxs("div", { className: "card space-y-2", children: [_jsxs("div", { className: "flex items-center justify-between", children: [_jsx("span", { className: "font-medium", children: a.action }), _jsxs("span", { className: "text-xs text-ink-400", children: [a.category, " \u00B7 ", when(a.requested_at)] })] }), _jsx("p", { className: "text-sm", children: a.reason }), _jsx("pre", { className: "overflow-x-auto rounded bg-ink-100 p-2 text-xs dark:bg-ink-900", children: JSON.stringify(a.details, null, 1) }), _jsxs("div", { className: "flex gap-2", children: [_jsx("button", { className: "btn", onClick: () => decide(a, true), children: "Approve" }), _jsx("button", { className: "btn-ghost", onClick: () => decide(a, false), children: "Deny" })] })] }, a.id))), !list.data?.length && _jsx(Empty, { children: "No approvals waiting." })] }));
}
