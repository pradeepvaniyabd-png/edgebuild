import { useEffect, useRef, useState } from "react";
const TOKEN_KEY = "sax.token";
export const getToken = () => localStorage.getItem(TOKEN_KEY) ?? "";
export const setToken = (value) => {
    if (value)
        localStorage.setItem(TOKEN_KEY, value);
    else
        localStorage.removeItem(TOKEN_KEY);
};
export class ApiError extends Error {
    status;
    hint;
    constructor(status, message, hint) {
        super(message);
        this.status = status;
        this.hint = hint;
    }
}
function headers(extra) {
    const token = getToken();
    return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra };
}
export async function api(path, init = {}) {
    const response = await fetch(path, {
        method: init.method ?? (init.body !== undefined ? "POST" : "GET"),
        headers: headers(init.body !== undefined ? { "Content-Type": "application/json" } : undefined),
        body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
    });
    if (!response.ok) {
        let detail = {};
        try {
            detail = await response.json();
        }
        catch {
            /* non-JSON error body */
        }
        throw new ApiError(response.status, detail.message ?? detail.detail ?? response.statusText, detail.hint);
    }
    return (await response.json());
}
export async function upload(file) {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch("/api/v1/uploads", { method: "POST", headers: headers(), body: form });
    if (!response.ok)
        throw new ApiError(response.status, (await response.json().catch(() => ({}))).detail ?? "upload failed");
    return response.json();
}
/** Poll an endpoint. Re-runs when `deps` change and stops when unmounted. */
export function usePoll(path, intervalMs = 3000) {
    const [data, setData] = useState(null);
    const [error, setError] = useState("");
    const [tick, setTick] = useState(0);
    useEffect(() => {
        if (!path) {
            setData(null);
            return;
        }
        let alive = true;
        let timer;
        const run = async () => {
            try {
                const next = await api(path);
                if (alive) {
                    setData(next);
                    setError("");
                }
            }
            catch (e) {
                if (alive)
                    setError(e instanceof Error ? e.message : String(e));
            }
            if (alive && intervalMs > 0)
                timer = window.setTimeout(run, intervalMs);
        };
        void run();
        return () => {
            alive = false;
            window.clearTimeout(timer);
        };
    }, [path, intervalMs, tick]);
    return { data, error, reload: () => setTick((n) => n + 1) };
}
/** Authenticated file preview: fetch with the token and expose an object URL. */
export function useBlobUrl(path) {
    const [url, setUrl] = useState(null);
    useEffect(() => {
        if (!path)
            return;
        let alive = true;
        let created = null;
        fetch(`/api/v1/files?path=${encodeURIComponent(path)}`, { headers: headers() })
            .then((r) => (r.ok ? r.blob() : Promise.reject(new Error(String(r.status)))))
            .then((blob) => {
            if (!alive)
                return;
            created = URL.createObjectURL(blob);
            setUrl(created);
        })
            .catch(() => alive && setUrl(null));
        return () => {
            alive = false;
            if (created)
                URL.revokeObjectURL(created);
        };
    }, [path]);
    return url;
}
/** Server-sent events over fetch, so the Authorization header can be sent (EventSource cannot). */
export function useEvents(executionId, onEvent) {
    const [events, setEvents] = useState([]);
    const callback = useRef(onEvent);
    callback.current = onEvent;
    useEffect(() => {
        setEvents([]);
        if (!executionId)
            return;
        const controller = new AbortController();
        (async () => {
            try {
                const response = await fetch(`/api/v1/tasks/${executionId}/events`, { headers: headers(), signal: controller.signal });
                if (!response.body)
                    return;
                const reader = response.body.getReader();
                const decoder = new TextDecoder();
                let buffer = "";
                for (;;) {
                    const { value, done } = await reader.read();
                    if (done)
                        break;
                    buffer += decoder.decode(value, { stream: true });
                    let index;
                    while ((index = buffer.indexOf("\n\n")) >= 0) {
                        const block = buffer.slice(0, index);
                        buffer = buffer.slice(index + 2);
                        const data = block.split("\n").find((l) => l.startsWith("data: "));
                        if (!data)
                            continue;
                        const event = JSON.parse(data.slice(6));
                        setEvents((prev) => [...prev, event]);
                        callback.current?.(event);
                    }
                }
            }
            catch {
                /* aborted or disconnected: polling keeps the view fresh */
            }
        })();
        return () => controller.abort();
    }, [executionId]);
    return events;
}
export const STATUS_STYLE = {
    SUCCESS: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
    COMPLETED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
    AVAILABLE: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
    APPROVED: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
    RUNNING: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200",
    GENERATING: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200",
    QC: "bg-indigo-100 text-indigo-800 dark:bg-indigo-900/40 dark:text-indigo-200",
    READY: "bg-sky-100 text-sky-800 dark:bg-sky-900/40 dark:text-sky-200",
    QUEUED: "bg-ink-100 text-ink-600 dark:bg-ink-800 dark:text-ink-200",
    PENDING: "bg-ink-100 text-ink-600 dark:bg-ink-800 dark:text-ink-200",
    PLANNING: "bg-ink-100 text-ink-600 dark:bg-ink-800 dark:text-ink-200",
    RETRYING: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    WAITING_APPROVAL: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    APPROVAL_REQUIRED: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    PARTIAL: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    RATE_LIMITED: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
    NOT_CONFIGURED: "bg-ink-100 text-ink-400 dark:bg-ink-800 dark:text-ink-400",
    FAILED: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
    BLOCKED: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
    DENIED: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
    AUTH_ERROR: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
    NETWORK_ERROR: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
    CANCELLED: "bg-ink-200 text-ink-600 dark:bg-ink-600 dark:text-ink-100",
    INTERRUPTED: "bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-200",
};
