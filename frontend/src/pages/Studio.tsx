import { useState } from "react";
import { api, usePoll, type Json } from "../api";
import { Badge, Bar, Empty, ErrorNote, Img, Page, Video } from "../components/ui";

export type Open = (page: string, execution?: string) => void;
const base = (pid: string) => `/api/v1/creative/projects/${pid}`;

function useProject(pid: string | null) {
  return usePoll<Json>(pid ? base(pid) : null, 2500);
}

function NeedProject() {
  return <Empty>Pick or create a project on the Projects page first.</Empty>;
}

export function Projects({ selected, select }: { selected: string | null; select: (id: string) => void }) {
  const list = usePoll<Json[]>("/api/v1/creative/projects", 4000);
  const [form, setForm] = useState({ idea: "", title: "", language: "hinglish", duration_seconds: 30, aspect_ratio: "9:16", style: "paper pop-up book" });
  const [error, setError] = useState("");
  const create = async () => {
    setError("");
    try {
      const made = await api<{ project_id: string }>("/api/v1/creative/projects", { body: form });
      select(made.project_id);
      setForm({ ...form, idea: "", title: "" });
      list.reload();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  return (
    <Page title="Projects" subtitle="A project holds the brief, story, characters, assets and renders for one video.">
      <div className="card grid gap-3 md:grid-cols-4">
        <div className="md:col-span-4"><label className="label">Idea</label><textarea className="input" value={form.idea} onChange={(e) => setForm({ ...form, idea: e.target.value })} placeholder="A paper chai-seller discovers a secret door…" /></div>
        <div><label className="label">Title (optional)</label><input className="input" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></div>
        <div><label className="label">Language</label><select className="input" value={form.language} onChange={(e) => setForm({ ...form, language: e.target.value })}><option value="hinglish">Hinglish</option><option value="hi">Hindi</option><option value="en">English</option></select></div>
        <div><label className="label">Seconds</label><input className="input" type="number" min={5} max={600} value={form.duration_seconds} onChange={(e) => setForm({ ...form, duration_seconds: Number(e.target.value) })} /></div>
        <div><label className="label">Aspect</label><select className="input" value={form.aspect_ratio} onChange={(e) => setForm({ ...form, aspect_ratio: e.target.value })}>{["9:16", "16:9", "1:1", "4:5"].map((a) => <option key={a}>{a}</option>)}</select></div>
        <div className="md:col-span-2"><label className="label">Style</label><select className="input" value={form.style} onChange={(e) => setForm({ ...form, style: e.target.value })}>{["cinematic", "photorealistic", "stylized 3D", "miniature diorama", "paper pop-up book", "clay animation", "cartoon", "anime", "comic", "documentary", "commercial", "fantasy", "educational"].map((s) => <option key={s}>{s}</option>)}</select></div>
        <div className="flex items-end gap-2 md:col-span-2"><button className="btn" disabled={form.idea.trim().length < 3} onClick={create}>Create project</button><ErrorNote message={error} /></div>
      </div>
      <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
        {(list.data ?? []).map((p) => (
          <button key={p.id} onClick={() => select(p.id)} className={`card text-left transition hover:border-brand-500 ${selected === p.id ? "border-brand-500" : ""}`}>
            <div className="flex items-center justify-between"><span className="font-medium">{p.name}</span><Badge status={p.status === "CREATED" ? "PENDING" : p.status === "EXPORTED" ? "SUCCESS" : "RUNNING"} /></div>
            <div className="mt-1 line-clamp-2 text-sm text-ink-400">{p.prompt}</div>
          </button>
        ))}
      </div>
      {!list.data?.length && <Empty>No projects yet.</Empty>}
    </Page>
  );
}

export function Storyboard({ pid, open }: { pid: string | null; open: Open }) {
  const project = useProject(pid);
  const [error, setError] = useState("");
  if (!pid) return <Page title="Storyboard"><NeedProject /></Page>;
  const m = project.data?.manifest;
  const run = async (step: string, body?: unknown) => {
    setError("");
    try {
      const started = await api<{ id: string }>(`${base(pid)}/${step}`, { method: "POST", body: body ?? {} });
      open("tasks", started.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const steps: [string, string][] = [["storyboard", "1 · Storyboard"], ["generate-images", "2 · Images"], ["voice", "3 · Voice"], ["generate-video", "4 · Clips"], ["render", "5 · Render"], ["qc", "6 · Quality check"]];
  return (
    <Page title={m?.story?.title ?? m?.name ?? "Storyboard"} subtitle={m?.story ? `${m.story.scenes.length} scenes · ${m.story.duration}s · written by ${m.story.method === "llm" ? "a language model" : "the built-in template"}` : "No storyboard yet."}
      actions={steps.map(([step, label]) => <button key={step} className="btn-ghost" onClick={() => run(step)}>{label}</button>)}>
      <ErrorNote message={error || project.error} />
      {m?.story?.method === "template" && <div className="rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-900/30 dark:text-amber-100">This storyboard is a generic template. Configure a language model (see Providers) for a written story.</div>}
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {(m?.story?.scenes ?? []).map((s: Json) => (
          <article key={s.scene_id} className="card space-y-2">
            <div className="flex items-center justify-between"><h3 className="font-medium">{s.scene_id}</h3><span className="text-xs text-ink-400">{s.duration}s · {s.camera}</span></div>
            <Img path={m.selected?.image?.[s.scene_id]} alt={s.action} className="aspect-[9/16] max-h-64 w-full rounded-lg object-cover" />
            <p className="text-sm">{s.action}</p>
            {s.narration && <p className="border-l-2 border-brand-500 pl-2 text-sm italic">{s.narration}</p>}
            <details className="text-xs text-ink-400"><summary className="cursor-pointer">Prompts</summary><p className="mt-1">{s.image_prompt}</p></details>
          </article>
        ))}
      </div>
    </Page>
  );
}

export function Characters({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="Characters"><NeedProject /></Page>;
  const m = project.data?.manifest;
  return (
    <Page title="Character bible" subtitle="Every scene prompt includes these descriptions so characters stay consistent.">
      <div className="grid gap-3 md:grid-cols-2">
        {(m?.story?.characters ?? []).map((c: Json) => (
          <article key={c.character_id} className="card space-y-1">
            <h3 className="font-medium">{c.name}</h3>
            <p className="text-sm">{c.visual_description}</p>
            <dl className="grid grid-cols-[7rem_1fr] gap-x-2 text-sm">
              {([["Clothing", c.clothing], ["Hair", c.hair], ["Proportions", c.body_proportions], ["Colours", (c.colors ?? []).join(", ")], ["Personality", c.personality]] as [string, string][]).filter(([, v]) => v).map(([k, v]) => (<div key={k} className="contents"><dt className="text-ink-400">{k}</dt><dd>{v}</dd></div>))}
            </dl>
          </article>
        ))}
      </div>
      {!m?.story?.characters?.length && <Empty>No characters yet. Create the storyboard first.</Empty>}
      {m?.style && <div className="card"><h3 className="mb-1 font-medium">Style bible</h3><p className="text-sm text-ink-600 dark:text-ink-200">{m.style.visual_style}: {m.style.rendering_style}. {m.style.lighting}; {m.style.color_language}.</p></div>}
    </Page>
  );
}

export function References({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="References"><NeedProject /></Page>;
  const root = project.data?.manifest?.project_id;
  const folders: Json = project.data?.assets ?? {};
  const files = [...(folders.references ?? []).map((f: string) => `references/${f}`), ...(folders.characters ?? []).filter((f: string) => f.endsWith(".png")).map((f: string) => `characters/${f}`)];
  return (
    <Page title="References" subtitle="Reference images and character sheets that condition generation where the provider supports it.">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {files.map((f) => <figure key={f} className="card p-2"><Img path={`projects/${root}/${f}`} alt={f} className="aspect-square w-full rounded object-cover" /><figcaption className="mt-1 truncate text-xs text-ink-400">{f}</figcaption></figure>)}
      </div>
      {!files.length && <Empty>No reference images yet.</Empty>}
    </Page>
  );
}

export function ImageGallery({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="Image gallery"><NeedProject /></Page>;
  const gens = ((project.data?.generations ?? []) as Json[]).filter((g) => g.kind === "image" && g.selected);
  const m = project.data?.manifest;
  return (
    <Page title="Image gallery" subtitle="The best-scoring take for each scene.">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5">
        {Object.entries((m?.selected?.image ?? {}) as Record<string, string>).map(([scene, path]) => {
          const g = gens.find((x) => x.scene_id === scene);
          return <figure key={scene} className="card p-2"><Img path={path} alt={scene} className="aspect-[9/16] w-full rounded object-cover" /><figcaption className="mt-1 flex justify-between text-xs"><span>{scene}</span><span className="text-ink-400">{g?.provider} · {g?.score?.toFixed(2)}</span></figcaption></figure>;
        })}
      </div>
      {!Object.keys(m?.selected?.image ?? {}).length && <Empty>No images generated yet.</Empty>}
    </Page>
  );
}

export function VideoGallery({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="Video gallery"><NeedProject /></Page>;
  const m = project.data?.manifest;
  const final = m?.outputs?.render?.path;
  return (
    <Page title="Video gallery" subtitle="Scene clips and the final render.">
      {final && <div className="card"><h3 className="mb-2 font-medium">Final render</h3><Video path={final} className="max-h-[70vh] rounded-lg" /><p className="mt-1 text-xs text-ink-400">{m.outputs.render.duration?.toFixed(1)}s · subtitles {m.outputs.render.subtitles ? "burned in" : "off"} · music: {m.outputs.render.music}</p></div>}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Object.entries((m?.selected?.video ?? {}) as Record<string, string>).map(([scene, path]) => <figure key={scene} className="card p-2"><Video path={path} className="aspect-[9/16] w-full rounded object-cover" /><figcaption className="mt-1 text-xs">{scene}</figcaption></figure>)}
      </div>
      {!final && !Object.keys(m?.selected?.video ?? {}).length && <Empty>No clips yet.</Empty>}
    </Page>
  );
}

export function Queue({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="Generation queue"><NeedProject /></Page>;
  const rows = (project.data?.generations ?? []) as Json[];
  return (
    <Page title="Generation queue" subtitle="Every attempt, with its provider and status. Starred rows were selected.">
      <div className="card overflow-x-auto p-0">
        <table className="w-full"><thead><tr>{["", "Scene", "Kind", "Status", "Provider", "Score", "Error"].map((h) => <th key={h} className="th">{h}</th>)}</tr></thead>
          <tbody>{rows.map((g) => <tr key={g.id} className="border-t border-ink-100 dark:border-ink-800"><td className="td">{g.selected ? "★" : ""}</td><td className="td">{g.scene_id}</td><td className="td">{g.kind}</td><td className="td"><Badge status={g.status} /></td><td className="td">{g.provider ?? "-"}</td><td className="td">{g.score != null ? g.score.toFixed(2) : "-"}</td><td className="td max-w-xs truncate text-red-600">{g.error}</td></tr>)}</tbody></table>
        {!rows.length && <div className="p-4"><Empty>Nothing queued yet.</Empty></div>}
      </div>
    </Page>
  );
}

export function Quality({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="Quality scores"><NeedProject /></Page>;
  const rows = ((project.data?.generations ?? []) as Json[]).filter((g) => g.score != null);
  const qc = project.data?.manifest?.outputs?.qc;
  return (
    <Page title="Quality scores" subtitle="Technical scores are exact. Prompt alignment and artefacts are only scored when a vision model is configured.">
      {qc && <div className="card flex items-center justify-between"><span>Final gates</span>{qc.final_ready ? <Badge status="SUCCESS" /> : <span className="text-sm text-red-600">failed: {(qc.failed ?? []).join(", ")}</span>}</div>}
      <div className="card space-y-2">
        {rows.map((g) => <div key={g.id} className="grid grid-cols-[8rem_1fr_3rem] items-center gap-2 text-sm"><span>{g.scene_id} {g.kind}{g.selected ? " ★" : ""}</span><Bar value={g.score} max={1} danger={g.score < 0.3} /><span className="text-right">{g.score.toFixed(2)}</span></div>)}
        {!rows.length && <Empty>No scored generations yet.</Empty>}
      </div>
    </Page>
  );
}

export function Exports({ pid }: { pid: string | null }) {
  const project = useProject(pid);
  if (!pid) return <Page title="Exports"><NeedProject /></Page>;
  const m = project.data?.manifest;
  const exp = m?.outputs?.export;
  const social = m?.outputs?.social;
  return (
    <Page title="Exports" subtitle="Exporting copies the finished video and metadata into the project's exports folder. Nothing is uploaded.">
      {exp ? <div className="card"><div className="font-medium">Exported</div><div className="text-sm text-ink-400">{exp.dir}</div></div> : <Empty>Not exported yet. The export step waits for your approval (see Approvals).</Empty>}
      {social && <div className="card space-y-1"><h3 className="font-medium">{social.title}</h3><p className="text-sm">{social.description}</p><p className="text-sm text-ink-400">{social.caption}</p><p className="text-sm">{(social.hashtags ?? []).join(" ")}</p><p className="text-xs text-ink-400">{social.publishing}</p></div>}
      {m?.outputs?.thumbnail && <div className="grid grid-cols-3 gap-3">{Object.entries(m.outputs.thumbnail as Record<string, string>).map(([k, v]) => <figure key={k} className="card p-2"><Img path={v} alt={k} className="w-full rounded" /><figcaption className="text-xs text-ink-400">{k}</figcaption></figure>)}</div>}
    </Page>
  );
}
