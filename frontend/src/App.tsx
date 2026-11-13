import { useEffect, useState } from "react";
import { getToken, setToken, usePoll, type Json } from "./api";
import { Overview } from "./pages/Overview";
import { Characters, Exports, ImageGallery, Projects, Quality, Queue, References, Storyboard, VideoGallery } from "./pages/Studio";
import { Approvals, Costs, Logs, Providers } from "./pages/System";
import { Tasks } from "./pages/Tasks";

const NAV: [string, string][] = [
  ["dashboard", "Dashboard"], ["tasks", "Tasks"], ["projects", "Projects"], ["storyboard", "Storyboard"], ["characters", "Characters"],
  ["references", "References"], ["images", "Image gallery"], ["videos", "Video gallery"], ["queue", "Generation queue"], ["quality", "Quality scores"],
  ["approvals", "Approvals"], ["exports", "Exports"], ["providers", "Providers"], ["costs", "Costs"], ["logs", "Live logs"],
];

const fromHash = () => window.location.hash.replace(/^#\/?/, "").split("/")[0] || "dashboard";

export default function App() {
  const [page, setPage] = useState(fromHash());
  const [execution, setExecution] = useState<string | null>(null);
  const [project, setProject] = useState<string | null>(localStorage.getItem("sax.project"));
  const [token, setTok] = useState(getToken());
  const [menu, setMenu] = useState(false);
  const stats = usePoll<Json>("/api/v1/stats", 5000);

  useEffect(() => {
    const onHash = () => setPage(fromHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const go = (next: string, id?: string) => {
    window.location.hash = `/${next}`;
    if (next === "tasks" && id) setExecution(id);
    setMenu(false);
  };
  const choose = (id: string) => {
    setProject(id);
    localStorage.setItem("sax.project", id);
  };

  const needsToken = stats.error.includes("401") || stats.error.toLowerCase().includes("token");
  const pending = stats.data?.approvals_pending ?? 0;
  const body = (() => {
    switch (page) {
      case "tasks": return <Tasks selected={execution} select={setExecution} />;
      case "projects": return <Projects selected={project} select={choose} />;
      case "storyboard": return <Storyboard pid={project} open={go} />;
      case "characters": return <Characters pid={project} />;
      case "references": return <References pid={project} />;
      case "images": return <ImageGallery pid={project} />;
      case "videos": return <VideoGallery pid={project} />;
      case "queue": return <Queue pid={project} />;
      case "quality": return <Quality pid={project} />;
      case "approvals": return <Approvals />;
      case "exports": return <Exports pid={project} />;
      case "providers": return <Providers />;
      case "costs": return <Costs />;
      case "logs": return <Logs />;
      default: return <Overview open={go} />;
    }
  })();

  return (
    <div className="min-h-screen md:flex">
      <header className="flex items-center justify-between border-b border-ink-200 bg-white px-4 py-3 md:hidden dark:border-ink-800 dark:bg-ink-900">
        <strong>Super Agent X</strong>
        <button className="btn-ghost" aria-expanded={menu} onClick={() => setMenu(!menu)}>Menu</button>
      </header>
      <nav className={`${menu ? "block" : "hidden"} w-full shrink-0 border-r border-ink-200 bg-white p-3 md:block md:w-56 dark:border-ink-800 dark:bg-ink-900`} aria-label="Main">
        <div className="mb-3 hidden px-2 text-lg font-semibold md:block">Super Agent X</div>
        <ul className="space-y-0.5">
          {NAV.map(([id, label]) => (
            <li key={id}>
              <button onClick={() => go(id)} aria-current={page === id ? "page" : undefined} className={`flex w-full items-center justify-between rounded-lg px-2 py-1.5 text-left text-sm hover:bg-ink-100 dark:hover:bg-ink-800 ${page === id ? "bg-ink-100 font-medium dark:bg-ink-800" : ""}`}>
                {label}
                {id === "approvals" && pending > 0 && <span className="rounded-full bg-amber-500 px-1.5 text-xs text-white">{pending}</span>}
              </button>
            </li>
          ))}
        </ul>
        {project && <p className="mt-3 truncate px-2 text-xs text-ink-400" title={project}>Project: {project}</p>}
        <div className="mt-4 border-t border-ink-100 pt-3 dark:border-ink-800">
          <label className="label px-2" htmlFor="token">API token</label>
          <input id="token" type="password" className="input" value={token} placeholder={needsToken ? "required" : "optional"} onChange={(e) => { setTok(e.target.value); setToken(e.target.value); }} />
        </div>
      </nav>
      <main className="min-w-0 flex-1 p-4 md:p-6">
        {needsToken && <div role="alert" className="mb-4 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-900/30 dark:text-amber-100">The server requires an API token. Enter it in the sidebar.</div>}
        {body}
      </main>
    </div>
  );
}
