import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
import { STATUS_STYLE, useBlobUrl } from "../api";
export function Badge({ status }) {
    return (_jsx("span", { className: `inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[status] ?? STATUS_STYLE.PENDING}`, children: status.replaceAll("_", " ") }));
}
export function Page({ title, subtitle, actions, children }) {
    return (_jsxs("section", { className: "space-y-4", children: [_jsxs("header", { className: "flex flex-wrap items-end justify-between gap-2", children: [_jsxs("div", { children: [_jsx("h1", { className: "text-xl font-semibold", children: title }), subtitle && _jsx("p", { className: "text-sm text-ink-400", children: subtitle })] }), _jsx("div", { className: "flex flex-wrap gap-2", children: actions })] }), children] }));
}
export function Empty({ children }) {
    return _jsx("div", { className: "card text-center text-sm text-ink-400", children: children });
}
export function ErrorNote({ message }) {
    if (!message)
        return null;
    return _jsx("div", { role: "alert", className: "rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-900/30 dark:text-red-200", children: message });
}
export function Stat({ label, value, hint }) {
    return (_jsxs("div", { className: "card", children: [_jsx("div", { className: "text-xs uppercase tracking-wide text-ink-400", children: label }), _jsx("div", { className: "mt-1 text-2xl font-semibold", children: value }), hint && _jsx("div", { className: "text-xs text-ink-400", children: hint })] }));
}
export function Bar({ value, max, danger }) {
    const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
    return (_jsx("div", { className: "h-2 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800", children: _jsx("div", { className: `h-full ${danger || pct > 90 ? "bg-red-500" : "bg-brand-500"}`, style: { width: `${pct}%` } }) }));
}
export function Img({ path, alt, className }) {
    const url = useBlobUrl(path);
    if (!path)
        return _jsx("div", { className: `flex items-center justify-center bg-ink-100 text-xs text-ink-400 dark:bg-ink-800 ${className ?? ""}`, children: "no image" });
    return url ? _jsx("img", { src: url, alt: alt, className: className, loading: "lazy" }) : _jsx("div", { className: `animate-pulse bg-ink-100 dark:bg-ink-800 ${className ?? ""}` });
}
export function Video({ path, className }) {
    const url = useBlobUrl(path);
    if (!path)
        return _jsx("div", { className: `flex items-center justify-center bg-ink-100 text-xs text-ink-400 dark:bg-ink-800 ${className ?? ""}`, children: "no clip" });
    return url ? _jsx("video", { src: url, controls: true, preload: "metadata", className: className }) : _jsx("div", { className: `animate-pulse bg-ink-100 dark:bg-ink-800 ${className ?? ""}` });
}
export const money = (value) => `$${(value ?? 0).toFixed(value && value < 1 ? 4 : 2)}`;
export const when = (value) => (value ? new Date(value).toLocaleString() : "-");
