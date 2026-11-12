import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { useState } from "react";
import { api, usePoll } from "../api";
import { Badge, Bar, Empty, ErrorNote, Img, Page, Video } from "../components/ui";
const base = (pid) => `/api/v1/creative/projects/${pid}`;
function useProject(pid) {
    return usePoll(pid ? base(pid) : null, 2500);
}
function NeedProject() {
    return _jsx(Empty, { children: "Pick or create a project on the Projects page first." });
}
export function Projects({ selected, select }) {
    const list = usePoll("/api/v1/creative/projects", 4000);
    const [form, setForm] = useState({ idea: "", title: "", language: "hinglish", duration_seconds: 30, aspect_ratio: "9:16", style: "paper pop-up book" });
    const [error, setError] = useState("");
    const create = async () => {
        setError("");
        try {
            const made = await api("/api/v1/creative/projects", { body: form });
            select(made.project_id);
            setForm({ ...form, idea: "", title: "" });
            list.reload();
        }
        catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };
    return (_jsxs(Page, { title: "Projects", subtitle: "A project holds the brief, story, characters, assets and renders for one video.", children: [_jsxs("div", { className: "card grid gap-3 md:grid-cols-4", children: [_jsxs("div", { className: "md:col-span-4", children: [_jsx("label", { className: "label", children: "Idea" }), _jsx("textarea", { className: "input", value: form.idea, onChange: (e) => setForm({ ...form, idea: e.target.value }), placeholder: "A paper chai-seller discovers a secret door\u2026" })] }), _jsxs("div", { children: [_jsx("label", { className: "label", children: "Title (optional)" }), _jsx("input", { className: "input", value: form.title, onChange: (e) => setForm({ ...form, title: e.target.value }) })] }), _jsxs("div", { children: [_jsx("label", { className: "label", children: "Language" }), _jsxs("select", { className: "input", value: form.language, onChange: (e) => setForm({ ...form, language: e.target.value }), children: [_jsx("option", { value: "hinglish", children: "Hinglish" }), _jsx("option", { value: "hi", children: "Hindi" }), _jsx("option", { value: "en", children: "English" })] })] }), _jsxs("div", { children: [_jsx("label", { className: "label", children: "Seconds" }), _jsx("input", { className: "input", type: "number", min: 5, max: 600, value: form.duration_seconds, onChange: (e) => setForm({ ...form, duration_seconds: Number(e.target.value) }) })] }), _jsxs("div", { children: [_jsx("label", { className: "label", children: "Aspect" }), _jsx("select", { className: "input", value: form.aspect_ratio, onChange: (e) => setForm({ ...form, aspect_ratio: e.target.value }), children: ["9:16", "16:9", "1:1", "4:5"].map((a) => _jsx("option", { children: a }, a)) })] }), _jsxs("div", { className: "md:col-span-2", children: [_jsx("label", { className: "label", children: "Style" }), _jsx("select", { className: "input", value: form.style, onChange: (e) => setForm({ ...form, style: e.target.value }), children: ["cinematic", "photorealistic", "stylized 3D", "miniature diorama", "paper pop-up book", "clay animation", "cartoon", "anime", "comic", "documentary", "commercial", "fantasy", "educational"].map((s) => _jsx("option", { children: s }, s)) })] }), _jsxs("div", { className: "flex items-end gap-2 md:col-span-2", children: [_jsx("button", { className: "btn", disabled: form.idea.trim().length < 3, onClick: create, children: "Create project" }), _jsx(ErrorNote, { message: error })] })] }), _jsx("div", { className: "grid gap-3 md:grid-cols-2 lg:grid-cols-3", children: (list.data ?? []).map((p) => (_jsxs("button", { onClick: () => select(p.id), className: `card text-left transition hover:border-brand-500 ${selected === p.id ? "border-brand-500" : ""}`, children: [_jsxs("div", { className: "flex items-center justify-between", children: [_jsx("span", { className: "font-medium", children: p.name }), _jsx(Badge, { status: p.status === "CREATED" ? "PENDING" : p.status === "EXPORTED" ? "SUCCESS" : "RUNNING" })] }), _jsx("div", { className: "mt-1 line-clamp-2 text-sm text-ink-400", children: p.prompt })] }, p.id))) }), !list.data?.length && _jsx(Empty, { children: "No projects yet." })] }));
}
export function Storyboard({ pid, open }) {
    const project = useProject(pid);
    const [error, setError] = useState("");
    if (!pid)
        return _jsx(Page, { title: "Storyboard", children: _jsx(NeedProject, {}) });
    const m = project.data?.manifest;
    const run = async (step, body) => {
        setError("");
        try {
            const started = await api(`${base(pid)}/${step}`, { method: "POST", body: body ?? {} });
            open("tasks", started.id);
        }
        catch (e) {
            setError(e instanceof Error ? e.message : String(e));
        }
    };
    const steps = [["storyboard", "1 · Storyboard"], ["generate-images", "2 · Images"], ["voice", "3 · Voice"], ["generate-video", "4 · Clips"], ["render", "5 · Render"], ["qc", "6 · Quality check"]];
    return (_jsxs(Page, { title: m?.story?.title ?? m?.name ?? "Storyboard", subtitle: m?.story ? `${m.story.scenes.length} scenes · ${m.story.duration}s · written by ${m.story.method === "llm" ? "a language model" : "the built-in template"}` : "No storyboard yet.", actions: steps.map(([step, label]) => _jsx("button", { className: "btn-ghost", onClick: () => run(step), children: label }, step)), children: [_jsx(ErrorNote, { message: error || project.error }), m?.story?.method === "template" && _jsx("div", { className: "rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:bg-amber-900/30 dark:text-amber-100", children: "This storyboard is a generic template. Configure a language model (see Providers) for a written story." }), _jsx("div", { className: "grid gap-3 md:grid-cols-2 xl:grid-cols-3", children: (m?.story?.scenes ?? []).map((s) => (_jsxs("article", { className: "card space-y-2", children: [_jsxs("div", { className: "flex items-center justify-between", children: [_jsx("h3", { className: "font-medium", children: s.scene_id }), _jsxs("span", { className: "text-xs text-ink-400", children: [s.duration, "s \u00B7 ", s.camera] })] }), _jsx(Img, { path: m.selected?.image?.[s.scene_id], alt: s.action, className: "aspect-[9/16] max-h-64 w-full rounded-lg object-cover" }), _jsx("p", { className: "text-sm", children: s.action }), s.narration && _jsx("p", { className: "border-l-2 border-brand-500 pl-2 text-sm italic", children: s.narration }), _jsxs("details", { className: "text-xs text-ink-400", children: [_jsx("summary", { className: "cursor-pointer", children: "Prompts" }), _jsx("p", { className: "mt-1", children: s.image_prompt })] })] }, s.scene_id))) })] }));
}
export function Characters({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "Characters", children: _jsx(NeedProject, {}) });
    const m = project.data?.manifest;
    return (_jsxs(Page, { title: "Character bible", subtitle: "Every scene prompt includes these descriptions so characters stay consistent.", children: [_jsx("div", { className: "grid gap-3 md:grid-cols-2", children: (m?.story?.characters ?? []).map((c) => (_jsxs("article", { className: "card space-y-1", children: [_jsx("h3", { className: "font-medium", children: c.name }), _jsx("p", { className: "text-sm", children: c.visual_description }), _jsx("dl", { className: "grid grid-cols-[7rem_1fr] gap-x-2 text-sm", children: [["Clothing", c.clothing], ["Hair", c.hair], ["Proportions", c.body_proportions], ["Colours", (c.colors ?? []).join(", ")], ["Personality", c.personality]].filter(([, v]) => v).map(([k, v]) => (_jsxs("div", { className: "contents", children: [_jsx("dt", { className: "text-ink-400", children: k }), _jsx("dd", { children: v })] }, k))) })] }, c.character_id))) }), !m?.story?.characters?.length && _jsx(Empty, { children: "No characters yet. Create the storyboard first." }), m?.style && _jsxs("div", { className: "card", children: [_jsx("h3", { className: "mb-1 font-medium", children: "Style bible" }), _jsxs("p", { className: "text-sm text-ink-600 dark:text-ink-200", children: [m.style.visual_style, ": ", m.style.rendering_style, ". ", m.style.lighting, "; ", m.style.color_language, "."] })] })] }));
}
export function References({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "References", children: _jsx(NeedProject, {}) });
    const root = project.data?.manifest?.project_id;
    const folders = project.data?.assets ?? {};
    const files = [...(folders.references ?? []).map((f) => `references/${f}`), ...(folders.characters ?? []).filter((f) => f.endsWith(".png")).map((f) => `characters/${f}`)];
    return (_jsxs(Page, { title: "References", subtitle: "Reference images and character sheets that condition generation where the provider supports it.", children: [_jsx("div", { className: "grid grid-cols-2 gap-3 md:grid-cols-4", children: files.map((f) => _jsxs("figure", { className: "card p-2", children: [_jsx(Img, { path: `projects/${root}/${f}`, alt: f, className: "aspect-square w-full rounded object-cover" }), _jsx("figcaption", { className: "mt-1 truncate text-xs text-ink-400", children: f })] }, f)) }), !files.length && _jsx(Empty, { children: "No reference images yet." })] }));
}
export function ImageGallery({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "Image gallery", children: _jsx(NeedProject, {}) });
    const gens = (project.data?.generations ?? []).filter((g) => g.kind === "image" && g.selected);
    const m = project.data?.manifest;
    return (_jsxs(Page, { title: "Image gallery", subtitle: "The best-scoring take for each scene.", children: [_jsx("div", { className: "grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5", children: Object.entries((m?.selected?.image ?? {})).map(([scene, path]) => {
                    const g = gens.find((x) => x.scene_id === scene);
                    return _jsxs("figure", { className: "card p-2", children: [_jsx(Img, { path: path, alt: scene, className: "aspect-[9/16] w-full rounded object-cover" }), _jsxs("figcaption", { className: "mt-1 flex justify-between text-xs", children: [_jsx("span", { children: scene }), _jsxs("span", { className: "text-ink-400", children: [g?.provider, " \u00B7 ", g?.score?.toFixed(2)] })] })] }, scene);
                }) }), !Object.keys(m?.selected?.image ?? {}).length && _jsx(Empty, { children: "No images generated yet." })] }));
}
export function VideoGallery({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "Video gallery", children: _jsx(NeedProject, {}) });
    const m = project.data?.manifest;
    const final = m?.outputs?.render?.path;
    return (_jsxs(Page, { title: "Video gallery", subtitle: "Scene clips and the final render.", children: [final && _jsxs("div", { className: "card", children: [_jsx("h3", { className: "mb-2 font-medium", children: "Final render" }), _jsx(Video, { path: final, className: "max-h-[70vh] rounded-lg" }), _jsxs("p", { className: "mt-1 text-xs text-ink-400", children: [m.outputs.render.duration?.toFixed(1), "s \u00B7 subtitles ", m.outputs.render.subtitles ? "burned in" : "off", " \u00B7 music: ", m.outputs.render.music] })] }), _jsx("div", { className: "grid grid-cols-2 gap-3 md:grid-cols-4", children: Object.entries((m?.selected?.video ?? {})).map(([scene, path]) => _jsxs("figure", { className: "card p-2", children: [_jsx(Video, { path: path, className: "aspect-[9/16] w-full rounded object-cover" }), _jsx("figcaption", { className: "mt-1 text-xs", children: scene })] }, scene)) }), !final && !Object.keys(m?.selected?.video ?? {}).length && _jsx(Empty, { children: "No clips yet." })] }));
}
export function Queue({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "Generation queue", children: _jsx(NeedProject, {}) });
    const rows = (project.data?.generations ?? []);
    return (_jsx(Page, { title: "Generation queue", subtitle: "Every attempt, with its provider and status. Starred rows were selected.", children: _jsxs("div", { className: "card overflow-x-auto p-0", children: [_jsxs("table", { className: "w-full", children: [_jsx("thead", { children: _jsx("tr", { children: ["", "Scene", "Kind", "Status", "Provider", "Score", "Error"].map((h) => _jsx("th", { className: "th", children: h }, h)) }) }), _jsx("tbody", { children: rows.map((g) => _jsxs("tr", { className: "border-t border-ink-100 dark:border-ink-800", children: [_jsx("td", { className: "td", children: g.selected ? "★" : "" }), _jsx("td", { className: "td", children: g.scene_id }), _jsx("td", { className: "td", children: g.kind }), _jsx("td", { className: "td", children: _jsx(Badge, { status: g.status }) }), _jsx("td", { className: "td", children: g.provider ?? "-" }), _jsx("td", { className: "td", children: g.score != null ? g.score.toFixed(2) : "-" }), _jsx("td", { className: "td max-w-xs truncate text-red-600", children: g.error })] }, g.id)) })] }), !rows.length && _jsx("div", { className: "p-4", children: _jsx(Empty, { children: "Nothing queued yet." }) })] }) }));
}
export function Quality({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "Quality scores", children: _jsx(NeedProject, {}) });
    const rows = (project.data?.generations ?? []).filter((g) => g.score != null);
    const qc = project.data?.manifest?.outputs?.qc;
    return (_jsxs(Page, { title: "Quality scores", subtitle: "Technical scores are exact. Prompt alignment and artefacts are only scored when a vision model is configured.", children: [qc && _jsxs("div", { className: "card flex items-center justify-between", children: [_jsx("span", { children: "Final gates" }), qc.final_ready ? _jsx(Badge, { status: "SUCCESS" }) : _jsxs("span", { className: "text-sm text-red-600", children: ["failed: ", (qc.failed ?? []).join(", ")] })] }), _jsxs("div", { className: "card space-y-2", children: [rows.map((g) => _jsxs("div", { className: "grid grid-cols-[8rem_1fr_3rem] items-center gap-2 text-sm", children: [_jsxs("span", { children: [g.scene_id, " ", g.kind, g.selected ? " ★" : ""] }), _jsx(Bar, { value: g.score, max: 1, danger: g.score < 0.3 }), _jsx("span", { className: "text-right", children: g.score.toFixed(2) })] }, g.id)), !rows.length && _jsx(Empty, { children: "No scored generations yet." })] })] }));
}
export function Exports({ pid }) {
    const project = useProject(pid);
    if (!pid)
        return _jsx(Page, { title: "Exports", children: _jsx(NeedProject, {}) });
    const m = project.data?.manifest;
    const exp = m?.outputs?.export;
    const social = m?.outputs?.social;
    return (_jsxs(Page, { title: "Exports", subtitle: "Exporting copies the finished video and metadata into the project's exports folder. Nothing is uploaded.", children: [exp ? _jsxs("div", { className: "card", children: [_jsx("div", { className: "font-medium", children: "Exported" }), _jsx("div", { className: "text-sm text-ink-400", children: exp.dir })] }) : _jsx(Empty, { children: "Not exported yet. The export step waits for your approval (see Approvals)." }), social && _jsxs("div", { className: "card space-y-1", children: [_jsx("h3", { className: "font-medium", children: social.title }), _jsx("p", { className: "text-sm", children: social.description }), _jsx("p", { className: "text-sm text-ink-400", children: social.caption }), _jsx("p", { className: "text-sm", children: (social.hashtags ?? []).join(" ") }), _jsx("p", { className: "text-xs text-ink-400", children: social.publishing })] }), m?.outputs?.thumbnail && _jsx("div", { className: "grid grid-cols-3 gap-3", children: Object.entries(m.outputs.thumbnail).map(([k, v]) => _jsxs("figure", { className: "card p-2", children: [_jsx(Img, { path: v, alt: k, className: "w-full rounded" }), _jsx("figcaption", { className: "text-xs text-ink-400", children: k })] }, k)) })] }));
}
