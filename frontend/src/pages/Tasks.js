import { jsx as _jsx, jsxs as _jsxs, Fragment as _Fragment } from "react/jsx-runtime";
import { useMemo, useState } from "react";
import { api, useEvents, usePoll } from "../api";
import { Badge, Empty, ErrorNote, Page } from "../components/ui";
function waves(tasks) {
    const depth = new Map();
    const byId = new Map(tasks.map((t) => [t.id, t]));
    const visit = (task, seen) => {
        if (depth.has(task.id))
            return depth.get(task.id);
        if (seen.has(task.id))
            return 0;
        seen.add(task.id);
        const d = 1 + Math.max(-1, ...task.dependencies.map((id) => (byId.get(id) ? visit(byId.get(id), seen) : -1)));
        depth.set(task.id, d);
        return d;
    };
    tasks.forEach((t) => visit(t, new Set()));
    const out = [];
    tasks.forEach((t) => (out[depth.get(t.id) ?? 0] ??= []).push(t));
    return out.filter(Boolean);
}
export function Tasks({ selected, select }) {
    const list = usePoll("/api/v1/tasks?limit=30", 3000);
    const detail = usePoll(selected ? `/api/v1/tasks/${selected}` : null, 1500);
    const events = useEvents(selected);
    const [error, setError] = useState("");
    const grid = useMemo(() => waves((detail.data?.tasks ?? [])), [detail.data]);
    const act = async (path, body) => {
        setError("");
        try {
            await api(path, { method: "POST", body: body ?? {} });
            detail.reload();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };
    const d = detail.data;
    return (_jsx(Page, { title: "Tasks & agent activity", subtitle: "Each column runs in parallel; a task starts only when everything to its left that it needs has succeeded.", children: _jsxs("div", { className: "grid gap-4 lg:grid-cols-[18rem_1fr]", children: [_jsxs("div", { className: "card max-h-[70vh] space-y-1 overflow-y-auto p-2", children: [(list.data ?? []).map((t) => (_jsxs("button", { onClick: () => select(t.id), className: `w-full rounded-lg px-2 py-2 text-left text-sm hover:bg-ink-100 dark:hover:bg-ink-800 ${selected === t.id ? "bg-ink-100 dark:bg-ink-800" : ""}`, children: [_jsx("div", { className: "truncate", children: t.command }), _jsx(Badge, { status: t.status })] }, t.id))), !list.data?.length && _jsx(Empty, { children: "No executions yet." })] }), _jsxs("div", { className: "space-y-3", children: [!d && _jsx(Empty, { children: "Select an execution to see its task graph." }), d && (_jsxs(_Fragment, { children: [_jsxs("div", { className: "card flex flex-wrap items-center justify-between gap-2", children: [_jsxs("div", { className: "min-w-0", children: [_jsx("div", { className: "truncate font-medium", children: d.command }), _jsx("div", { className: "text-xs text-ink-400", children: d.id })] }), _jsxs("div", { className: "flex items-center gap-2", children: [_jsx(Badge, { status: d.status }), d.running && _jsx("button", { className: "btn-ghost", onClick: () => act(`/api/v1/tasks/${d.id}/cancel`), children: "Cancel" }), !d.running && ["FAILED", "PARTIAL", "INTERRUPTED", "WAITING_APPROVAL"].includes(d.status) && (_jsx("button", { className: "btn-ghost", onClick: () => act(`/api/v1/tasks/${d.id}/resume`), children: "Resume" }))] })] }), _jsx(ErrorNote, { message: error || detail.error }), d.approvals.filter((a) => a.status === "PENDING").map((a) => (_jsxs("div", { className: "card border-amber-300 bg-amber-50 dark:bg-amber-900/20", children: [_jsxs("div", { className: "text-sm font-medium", children: ["Approval needed: ", a.action] }), _jsx("div", { className: "text-sm", children: a.reason }), _jsx("pre", { className: "mt-1 overflow-x-auto text-xs text-ink-600 dark:text-ink-200", children: JSON.stringify(a.details, null, 1) }), _jsxs("div", { className: "mt-2 flex gap-2", children: [_jsx("button", { className: "btn", onClick: () => act(`/api/v1/tasks/${d.id}/approve`, { approval_id: a.id, approve: true }), children: "Approve" }), _jsx("button", { className: "btn-ghost", onClick: () => act(`/api/v1/tasks/${d.id}/approve`, { approval_id: a.id, approve: false }), children: "Deny" })] })] }, a.id))), _jsx("div", { className: "flex gap-3 overflow-x-auto pb-2", children: grid.map((column, i) => (_jsx("div", { className: "w-60 shrink-0 space-y-2", children: column.map((t) => (_jsxs("div", { className: "card p-3", children: [_jsxs("div", { className: "flex items-center justify-between gap-2", children: [_jsxs("span", { className: "truncate text-sm font-medium", children: [t.agent, ".", t.action] }), _jsx(Badge, { status: t.status })] }), _jsx("div", { className: "mt-1 text-xs text-ink-400", children: t.description }), (t.metadata?.summary || t.error) && _jsx("div", { className: `mt-1 text-xs ${t.error ? "text-red-600" : ""}`, children: String(t.error ?? t.metadata.summary).slice(0, 160) }), t.retry_count > 0 && _jsxs("div", { className: "mt-1 text-xs text-amber-600", children: ["recovered ", t.retry_count, "\u00D7"] })] }, t.id))) }, i))) }), _jsxs("div", { className: "card max-h-56 overflow-y-auto font-mono text-xs", children: [events.slice(-60).map((e) => (_jsxs("div", { children: [_jsx("span", { className: "text-ink-400", children: e.type }), " ", e.payload?.agent ? `${e.payload.agent}.${e.payload.action}` : "", " ", e.payload?.error ?? e.payload?.summary ?? ""] }, e.id))), !events.length && _jsx("span", { className: "text-ink-400", children: "Waiting for events\u2026" })] })] }))] })] }) }));
}
