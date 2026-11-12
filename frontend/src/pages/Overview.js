import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from "react";
import { api, usePoll } from "../api";
import { Badge, Empty, ErrorNote, Page, Stat, money, when } from "../components/ui";
export function Overview({ open }) {
    const stats = usePoll("/api/v1/stats", 4000);
    const tasks = usePoll("/api/v1/tasks?limit=8", 3000);
    const providers = usePoll("/api/v1/providers", 15000);
    const costs = usePoll("/api/v1/costs", 10000);
    const [command, setCommand] = useState("");
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState("");
    const submit = async () => {
        setBusy(true);
        setError("");
        try {
            const created = await api("/api/v1/tasks", { body: { command } });
            setCommand("");
            open("tasks", created.id);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
        finally {
            setBusy(false);
        }
    };
    const ready = (providers.data ?? []).filter((p) => p.status === "AVAILABLE");
    return (_jsxs(Page, { title: "Dashboard", subtitle: "Describe what you want in English, Hindi or Hinglish. Super Agent X plans it, delegates, verifies and reports.", children: [_jsxs("div", { className: "card space-y-2", children: [_jsx("label", { className: "label", htmlFor: "command", children: "New command" }), _jsx("textarea", { id: "command", className: "input min-h-[84px]", value: command, onChange: (e) => setCommand(e.target.value), placeholder: "e.g. pension.xlsx ka analysis karo aur report banao   |   Create a 30-second funny paper pop-up-book video in Hinglish" }), _jsxs("div", { className: "flex items-center gap-2", children: [_jsx("button", { className: "btn", disabled: busy || !command.trim(), onClick: submit, children: busy ? "Starting…" : "Run" }), _jsx(ErrorNote, { message: error })] })] }), _jsxs("div", { className: "grid grid-cols-2 gap-3 md:grid-cols-4", children: [_jsx(Stat, { label: "Executions", value: stats.data?.executions ?? "-" }), _jsx(Stat, { label: "Pending approvals", value: stats.data?.approvals_pending ?? "-", hint: "need your decision" }), _jsx(Stat, { label: "Spend today", value: money(costs.data?.today_usd), hint: `budget ${money(costs.data?.budgets?.daily_usd)}` }), _jsx(Stat, { label: "Providers ready", value: `${ready.length}/${providers.data?.length ?? "-"}`, hint: ready.map((p) => p.name).slice(0, 4).join(", ") })] }), _jsxs("div", { className: "card overflow-x-auto p-0", children: [_jsxs("table", { className: "w-full", children: [_jsx("thead", { children: _jsxs("tr", { children: [_jsx("th", { className: "th", children: "Command" }), _jsx("th", { className: "th", children: "Status" }), _jsx("th", { className: "th", children: "Started" })] }) }), _jsx("tbody", { children: (tasks.data ?? []).map((t) => (_jsxs("tr", { className: "cursor-pointer border-t border-ink-100 hover:bg-ink-50 dark:border-ink-800 dark:hover:bg-ink-800/50", onClick: () => open("tasks", t.id), children: [_jsx("td", { className: "td max-w-xl truncate", children: t.command }), _jsx("td", { className: "td", children: _jsx(Badge, { status: t.status }) }), _jsx("td", { className: "td text-ink-400", children: when(t.created_at) })] }, t.id))) })] }), !tasks.data?.length && _jsx("div", { className: "p-4", children: _jsx(Empty, { children: "Nothing has run yet. Try a command above." }) })] })] }));
}
