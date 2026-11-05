import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, type Meta, type Source } from "./api";

// ---------------------------------------------------------------- contexts

export const MetaContext = createContext<Meta | null>(null);
export function useMeta(): Meta {
  const m = useContext(MetaContext);
  if (!m) throw new Error("meta not loaded");
  return m;
}

type Toast = { id: number; msg: string; kind: "ok" | "error" };
const ToastContext = createContext<(msg: string, kind?: "ok" | "error") => void>(() => {});
export const useToast = () => useContext(ToastContext);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const seq = useRef(0);
  const notify = useCallback((msg: string, kind: "ok" | "error" = "ok") => {
    const id = ++seq.current;
    setToasts((t) => [...t, { id, msg, kind }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), kind === "error" ? 9000 : 4500);
  }, []);
  return (
    <ToastContext.Provider value={notify}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`flash ${t.kind}`} onClick={() => setToasts((x) => x.filter((y) => y.id !== t.id))}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

// ---------------------------------------------------------------- data loading

export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let live = true;
    setLoading(true);
    fn()
      .then((d) => live && (setData(d), setError(null)))
      .catch((e: Error) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [...deps, tick]);
  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}

export function Loading({ error, loading }: { error: string | null; loading: boolean }) {
  if (error) return <div className="flash error">{error}</div>;
  if (loading) return <p className="muted">Loading…</p>;
  return null;
}

/** Button that runs an async action, shows progress, and reports the outcome. */
export function Act({ run, children, ok, className, confirm, disabled, title }: {
  run: () => Promise<unknown>; children: ReactNode; ok?: string | ((r: unknown) => string); className?: string;
  confirm?: string; disabled?: boolean; title?: string;
}) {
  const notify = useToast();
  const [busy, setBusy] = useState(false);
  return (
    <button type="button" className={className} disabled={busy || disabled} title={title} onClick={async () => {
      if (confirm && !window.confirm(confirm)) return;
      setBusy(true);
      try {
        const r = await run();
        if (ok) notify(typeof ok === "function" ? ok(r) : ok);
      } catch (e) {
        notify(e instanceof ApiError || e instanceof Error ? e.message : String(e), "error");
      } finally {
        setBusy(false);
      }
    }}>
      {busy ? "…" : children}
    </button>
  );
}

// ---------------------------------------------------------------- formatting

const fmtCache = new Map<string, Intl.DateTimeFormat>();
export function fmtTime(iso: string | null | undefined, tz: string, style: "full" | "time" | "short" | "date" = "short"): string {
  if (!iso) return "—";
  const key = `${tz}|${style}`;
  let f = fmtCache.get(key);
  if (!f) {
    const opts: Intl.DateTimeFormatOptions = { timeZone: tz, hour12: false };
    if (style === "time") Object.assign(opts, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    else if (style === "date") Object.assign(opts, { weekday: "short", day: "2-digit", month: "short" });
    else if (style === "full") Object.assign(opts, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    else Object.assign(opts, { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
    f = new Intl.DateTimeFormat("en-GB", opts);
    fmtCache.set(key, f);
  }
  return f.format(new Date(iso));
}

export function useTime() {
  const tz = useMeta().timezone;
  return (iso: string | null | undefined, style?: "full" | "time" | "short" | "date") => fmtTime(iso, tz, style);
}

export function fmtDur(sec: number | null | undefined): string {
  if (!sec) return "—";
  const s = Math.round(sec);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1024 && i < u.length - 1) (v /= 1024), i++;
  return `${v.toFixed(v < 10 && i ? 1 : 0)} ${u[i]}`;
}

export const label = (s: string) => s.replace(/_/g, " ").toLowerCase();

// ---------------------------------------------------------------- chips

const STATUS_CLS: Record<string, string> = { APPROVED: "ok", PENDING_REVIEW: "warn", REJECTED: "bad", QUARANTINED: "bad", UNAVAILABLE: "bad" };
const ELIG_CLS: Record<string, string> = { IPTV_READY: "ok", IPTV_NEEDS_TRANSCODE: "ok", EMBED_ONLY: "warn", REFERENCE_ONLY: "warn", UNAVAILABLE: "bad" };
const RIGHTS_CLS: Record<string, string> = { AUTHORISED: "ok", CATALOGUE_ONLY: "warn", RESTRICTED: "bad" };

export const Chip = ({ cls, children, title }: { cls?: string; children: ReactNode; title?: string }) => (
  <span className={`chip ${cls ?? ""}`} title={title}>{children}</span>
);
export const StatusChip = ({ s }: { s: string }) => <Chip cls={STATUS_CLS[s]}>{label(s)}</Chip>;
export const EligChip = ({ e }: { e: string }) => <Chip cls={ELIG_CLS[e]}>{label(e)}</Chip>;
export const RightsChip = ({ r }: { r: string }) => <Chip cls={RIGHTS_CLS[r]}>rights: {label(r)}</Chip>;
export const IptvChip = ({ st }: { st: string }) => (
  <Chip cls={st === "eligible" ? "ok" : "warn"} title="IPTV schedule eligibility">IPTV: {st}</Chip>
);

// ---------------------------------------------------------------- preview

/** Official embedded players for YouTube/Archive, HTML5 video otherwise. */
export function Preview({ sources, thumb }: { sources: Source[]; thumb?: string | null }) {
  const embed = sources.find((p) => p.type === "youtube_embed" || p.type === "archive_embed");
  if (embed?.url)
    return (
      <div className="preview">
        <iframe src={embed.url} loading="lazy" allow="encrypted-media; picture-in-picture; fullscreen" allowFullScreen
          referrerPolicy="strict-origin-when-cross-origin" title="preview" />
      </div>
    );
  const direct = sources.find((p) => p.type === "direct_url" || p.type === "local_file");
  if (direct)
    return (
      <div className="preview">
        <video controls preload={direct.type === "local_file" ? "metadata" : "none"} poster={thumb ?? undefined}
          src={direct.type === "local_file" ? `/admin/preview/local/${direct.id}` : direct.url ?? undefined} />
      </div>
    );
  return thumb ? <img className="thumb" src={thumb} alt="" loading="lazy" /> : null;
}

export function Field({ label: l, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <label>
      <span>{l}</span>
      {children}
      {hint && <span className="small muted">{hint}</span>}
    </label>
  );
}
