import type { ReactNode } from "react";
import { STATUS_STYLE, useBlobUrl } from "../api";

export function Badge({ status }: { status: string }) {
  return (
    <span className={`inline-block rounded-full px-2 py-0.5 text-xs font-medium ${STATUS_STYLE[status] ?? STATUS_STYLE.PENDING}`}>
      {status.replaceAll("_", " ")}
    </span>
  );
}

export function Page({ title, subtitle, actions, children }: { title: string; subtitle?: string; actions?: ReactNode; children: ReactNode }) {
  return (
    <section className="space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">{title}</h1>
          {subtitle && <p className="text-sm text-ink-400">{subtitle}</p>}
        </div>
        <div className="flex flex-wrap gap-2">{actions}</div>
      </header>
      {children}
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="card text-center text-sm text-ink-400">{children}</div>;
}

export function ErrorNote({ message }: { message: string }) {
  if (!message) return null;
  return <div role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-900/30 dark:text-red-200">{message}</div>;
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
  return (
    <div className="card">
      <div className="text-xs uppercase tracking-wide text-ink-400">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      {hint && <div className="text-xs text-ink-400">{hint}</div>}
    </div>
  );
}

export function Bar({ value, max, danger }: { value: number; max: number; danger?: boolean }) {
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-ink-100 dark:bg-ink-800">
      <div className={`h-full ${danger || pct > 90 ? "bg-red-500" : "bg-brand-500"}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Img({ path, alt, className }: { path?: string | null; alt: string; className?: string }) {
  const url = useBlobUrl(path);
  if (!path) return <div className={`flex items-center justify-center bg-ink-100 text-xs text-ink-400 dark:bg-ink-800 ${className ?? ""}`}>no image</div>;
  return url ? <img src={url} alt={alt} className={className} loading="lazy" /> : <div className={`animate-pulse bg-ink-100 dark:bg-ink-800 ${className ?? ""}`} />;
}

export function Video({ path, className }: { path?: string | null; className?: string }) {
  const url = useBlobUrl(path);
  if (!path) return <div className={`flex items-center justify-center bg-ink-100 text-xs text-ink-400 dark:bg-ink-800 ${className ?? ""}`}>no clip</div>;
  return url ? <video src={url} controls preload="metadata" className={className} /> : <div className={`animate-pulse bg-ink-100 dark:bg-ink-800 ${className ?? ""}`} />;
}

export const money = (value: number | null | undefined): string => `$${(value ?? 0).toFixed(value && value < 1 ? 4 : 2)}`;
export const when = (value: string | null | undefined): string => (value ? new Date(value).toLocaleString() : "-");
