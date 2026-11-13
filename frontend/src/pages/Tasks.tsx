import { useMemo, useState } from "react";
import { api, useEvents, usePoll, type Json } from "../api";
import { Badge, Empty, ErrorNote, Page } from "../components/ui";

interface TaskRow extends Json {
  id: string;
  dependencies: string[];
}

function waves(tasks: TaskRow[]): TaskRow[][] {
  const depth = new Map<string, number>();
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const visit = (task: TaskRow, seen: Set<string>): number => {
    if (depth.has(task.id)) return depth.get(task.id)!;
    if (seen.has(task.id)) return 0;
    seen.add(task.id);
    const d = 1 + Math.max(-1, ...task.dependencies.map((id) => (byId.get(id) ? visit(byId.get(id)!, seen) : -1)));
    depth.set(task.id, d);
    return d;
  };
  tasks.forEach((t) => visit(t, new Set()));
  const out: TaskRow[][] = [];
  tasks.forEach((t) => (out[depth.get(t.id) ?? 0] ??= []).push(t));
  return out.filter(Boolean);
}

export function Tasks({ selected, select }: { selected: string | null; select: (id: string | null) => void }) {
  const list = usePoll<Json[]>("/api/v1/tasks?limit=30", 3000);
  const detail = usePoll<Json>(selected ? `/api/v1/tasks/${selected}` : null, 1500);
  const events = useEvents(selected);
  const [error, setError] = useState("");
  const grid = useMemo(() => waves((detail.data?.tasks ?? []) as TaskRow[]), [detail.data]);

  const act = async (path: string, body?: unknown) => {
    setError("");
    try {
      await api(path, { method: "POST", body: body ?? {} });
      detail.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const d = detail.data;
  return (
    <Page title="Tasks & agent activity" subtitle="Each column runs in parallel; a task starts only when everything to its left that it needs has succeeded.">
      <div className="grid gap-4 lg:grid-cols-[18rem_1fr]">
        <div className="card max-h-[70vh] space-y-1 overflow-y-auto p-2">
          {(list.data ?? []).map((t) => (
            <button key={t.id} onClick={() => select(t.id)} className={`w-full rounded-lg px-2 py-2 text-left text-sm hover:bg-ink-100 dark:hover:bg-ink-800 ${selected === t.id ? "bg-ink-100 dark:bg-ink-800" : ""}`}>
              <div className="truncate">{t.command}</div>
              <Badge status={t.status} />
            </button>
          ))}
          {!list.data?.length && <Empty>No executions yet.</Empty>}
        </div>

        <div className="space-y-3">
          {!d && <Empty>Select an execution to see its task graph.</Empty>}
          {d && (
            <>
              <div className="card flex flex-wrap items-center justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate font-medium">{d.command}</div>
                  <div className="text-xs text-ink-400">{d.id}</div>
                </div>
                <div className="flex items-center gap-2">
                  <Badge status={d.status} />
                  {d.running && <button className="btn-ghost" onClick={() => act(`/api/v1/tasks/${d.id}/cancel`)}>Cancel</button>}
                  {!d.running && ["FAILED", "PARTIAL", "INTERRUPTED", "WAITING_APPROVAL"].includes(d.status) && (
                    <button className="btn-ghost" onClick={() => act(`/api/v1/tasks/${d.id}/resume`)}>Resume</button>
                  )}
                </div>
              </div>
              <ErrorNote message={error || detail.error} />
              {(d.approvals as Json[]).filter((a) => a.status === "PENDING").map((a) => (
                <div key={a.id} className="card border-amber-300 bg-amber-50 dark:bg-amber-900/20">
                  <div className="text-sm font-medium">Approval needed: {a.action}</div>
                  <div className="text-sm">{a.reason}</div>
                  <pre className="mt-1 overflow-x-auto text-xs text-ink-600 dark:text-ink-200">{JSON.stringify(a.details, null, 1)}</pre>
                  <div className="mt-2 flex gap-2">
                    <button className="btn" onClick={() => act(`/api/v1/tasks/${d.id}/approve`, { approval_id: a.id, approve: true })}>Approve</button>
                    <button className="btn-ghost" onClick={() => act(`/api/v1/tasks/${d.id}/approve`, { approval_id: a.id, approve: false })}>Deny</button>
                  </div>
                </div>
              ))}
              <div className="flex gap-3 overflow-x-auto pb-2">
                {grid.map((column, i) => (
                  <div key={i} className="w-60 shrink-0 space-y-2">
                    {column.map((t) => (
                      <div key={t.id} className="card p-3">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-sm font-medium">{t.agent}.{t.action}</span>
                          <Badge status={t.status} />
                        </div>
                        <div className="mt-1 text-xs text-ink-400">{t.description}</div>
                        {(t.metadata?.summary || t.error) && <div className={`mt-1 text-xs ${t.error ? "text-red-600" : ""}`}>{String(t.error ?? t.metadata.summary).slice(0, 160)}</div>}
                        {t.retry_count > 0 && <div className="mt-1 text-xs text-amber-600">recovered {t.retry_count}×</div>}
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div className="card max-h-56 overflow-y-auto font-mono text-xs">
                {events.slice(-60).map((e) => (
                  <div key={e.id}><span className="text-ink-400">{e.type}</span> {e.payload?.agent ? `${e.payload.agent}.${e.payload.action}` : ""} {e.payload?.error ?? e.payload?.summary ?? ""}</div>
                ))}
                {!events.length && <span className="text-ink-400">Waiting for events…</span>}
              </div>
            </>
          )}
        </div>
      </div>
    </Page>
  );
}
