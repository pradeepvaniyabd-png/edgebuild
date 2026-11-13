import { useState } from "react";
import { api, usePoll, type Json } from "../api";
import { Badge, Bar, Empty, ErrorNote, Page, money, when } from "../components/ui";

export function Providers() {
  const [probe, setProbe] = useState(false);
  const data = usePoll<Json[]>(`/api/v1/providers?probe=${probe}`, probe ? 0 : 10000);
  return (
    <Page title="Providers" subtitle="Local first, then free, then paid cloud. Anything unconfigured shows what to set." actions={<button className="btn-ghost" onClick={() => setProbe(true)}>Test connections</button>}>
      <div className="card overflow-x-auto p-0">
        <table className="w-full"><thead><tr>{["Kind", "Provider", "Tier", "Status", "Detail"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{(data.data ?? []).map((p) => <tr key={p.name} className="border-t border-ink-100 dark:border-ink-800"><td className="td">{p.kind}</td><td className="td font-medium">{p.name}</td><td className="td text-ink-400">{String(p.tier).replaceAll("_", " ")}</td><td className="td"><Badge status={p.status} /></td><td className="td text-ink-400">{p.detail}{p.cooldown_seconds > 0 ? ` (cooling down ${Math.round(p.cooldown_seconds)}s)` : ""}</td></tr>)}</tbody></table>
      </div>
      <ErrorNote message={data.error} />
    </Page>
  );
}

export function Costs() {
  const costs = usePoll<Json>("/api/v1/costs", 8000);
  const b = costs.data?.budgets ?? {};
  return (
    <Page title="Costs" subtitle="Local and free providers cost nothing. Paid calls over a budget wait for your approval.">
      <div className="card space-y-3">
        <div><div className="mb-1 flex justify-between text-sm"><span>Today</span><span>{money(costs.data?.today_usd)} / {money(b.daily_usd)}</span></div><Bar value={costs.data?.today_usd ?? 0} max={b.daily_usd ?? 0} /></div>
        <div className="text-sm text-ink-400">Per project {money(b.project_usd)} · per generation {money(b.per_generation_usd)} · all time {money(costs.data?.total_usd)}</div>
      </div>
      <div className="card overflow-x-auto p-0">
        <table className="w-full"><thead><tr>{["Provider", "Calls", "Spend", "Unpriced calls"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{Object.entries((costs.data?.by_provider ?? {}) as Record<string, Json>).map(([name, v]) => <tr key={name} className="border-t border-ink-100 dark:border-ink-800"><td className="td">{name}</td><td className="td">{v.calls}</td><td className="td">{money(v.usd)}</td><td className="td">{v.unpriced_calls}</td></tr>)}</tbody></table>
        {!Object.keys(costs.data?.by_provider ?? {}).length && <div className="p-4"><Empty>No usage recorded yet.</Empty></div>}
      </div>
      {costs.data?.note && <p className="text-xs text-ink-400">{costs.data.note}</p>}
    </Page>
  );
}

export function Logs() {
  const logs = usePoll<Json[]>("/api/v1/logs?limit=300", 2500);
  const [level, setLevel] = useState("");
  const rows = (logs.data ?? []).filter((r) => !level || r.level === level).slice(-200);
  return (
    <Page title="Live logs" subtitle="Structured, secret-masked server log." actions={<select aria-label="Level" className="input w-32" value={level} onChange={(e) => setLevel(e.target.value)}><option value="">All</option><option>INFO</option><option>WARNING</option><option>ERROR</option></select>}>
      <div className="card max-h-[70vh] overflow-y-auto font-mono text-xs">
        {rows.map((r, i) => <div key={i} className={r.level === "ERROR" ? "text-red-600" : r.level === "WARNING" ? "text-amber-600" : ""}><span className="text-ink-400">{String(r.ts).slice(11, 19)}</span> {r.level} {r.agent ? `[${r.agent}${r.tool ? "/" + r.tool : ""}] ` : ""}{r.msg}</div>)}
        {!rows.length && <span className="text-ink-400">No log lines yet.</span>}
      </div>
    </Page>
  );
}

export function Approvals() {
  const list = usePoll<Json[]>("/api/v1/approvals?status=PENDING", 2500);
  const [error, setError] = useState("");
  const decide = async (a: Json, approve: boolean) => {
    setError("");
    try {
      await api(`/api/v1/approvals/${a.id}`, { body: { approve } });
      list.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <Page title="Approvals" subtitle="Risky or paid actions wait here. Nothing happens until you decide.">
      <ErrorNote message={error} />
      {(list.data ?? []).map((a) => (
        <div key={a.id} className="card space-y-2">
          <div className="flex items-center justify-between"><span className="font-medium">{a.action}</span><span className="text-xs text-ink-400">{a.category} · {when(a.requested_at)}</span></div>
          <p className="text-sm">{a.reason}</p>
          <pre className="overflow-x-auto rounded bg-ink-100 p-2 text-xs dark:bg-ink-900">{JSON.stringify(a.details, null, 1)}</pre>
          <div className="flex gap-2"><button className="btn" onClick={() => decide(a, true)}>Approve</button><button className="btn-ghost" onClick={() => decide(a, false)}>Deny</button></div>
        </div>
      ))}
      {!list.data?.length && <Empty>No approvals waiting.</Empty>}
    </Page>
  );
}
