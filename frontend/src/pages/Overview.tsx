import { useState } from "react";
import { api, usePoll, type Json } from "../api";
import { Badge, Empty, ErrorNote, Page, Stat, money, when } from "../components/ui";

export function Overview({ open }: { open: (page: string, execution?: string) => void }) {
  const stats = usePoll<Json>("/api/v1/stats", 4000);
  const tasks = usePoll<Json[]>("/api/v1/tasks?limit=8", 3000);
  const providers = usePoll<Json[]>("/api/v1/providers", 15000);
  const costs = usePoll<Json>("/api/v1/costs", 10000);
  const [command, setCommand] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const created = await api<{ id: string }>("/api/v1/tasks", { body: { command } });
      setCommand("");
      open("tasks", created.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const ready = (providers.data ?? []).filter((p) => p.status === "AVAILABLE");
  return (
    <Page title="Dashboard" subtitle="Describe what you want in English, Hindi or Hinglish. Super Agent X plans it, delegates, verifies and reports.">
      <div className="card space-y-2">
        <label className="label" htmlFor="command">New command</label>
        <textarea
          id="command"
          className="input min-h-[84px]"
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          placeholder="e.g. pension.xlsx ka analysis karo aur report banao   |   Create a 30-second funny paper pop-up-book video in Hinglish"
        />
        <div className="flex items-center gap-2">
          <button className="btn" disabled={busy || !command.trim()} onClick={submit}>{busy ? "Starting…" : "Run"}</button>
          <ErrorNote message={error} />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Stat label="Executions" value={stats.data?.executions ?? "-"} />
        <Stat label="Pending approvals" value={stats.data?.approvals_pending ?? "-"} hint="need your decision" />
        <Stat label="Spend today" value={money(costs.data?.today_usd)} hint={`budget ${money(costs.data?.budgets?.daily_usd)}`} />
        <Stat label="Providers ready" value={`${ready.length}/${providers.data?.length ?? "-"}`} hint={ready.map((p) => p.name).slice(0, 4).join(", ")} />
      </div>

      <div className="card overflow-x-auto p-0">
        <table className="w-full">
          <thead><tr><th className="th">Command</th><th className="th">Status</th><th className="th">Started</th></tr></thead>
          <tbody>
            {(tasks.data ?? []).map((t) => (
              <tr key={t.id} className="cursor-pointer border-t border-ink-100 hover:bg-ink-50 dark:border-ink-800 dark:hover:bg-ink-800/50" onClick={() => open("tasks", t.id)}>
                <td className="td max-w-xl truncate">{t.command}</td>
                <td className="td"><Badge status={t.status} /></td>
                <td className="td text-ink-400">{when(t.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!tasks.data?.length && <div className="p-4"><Empty>Nothing has run yet. Try a command above.</Empty></div>}
      </div>
    </Page>
  );
}
