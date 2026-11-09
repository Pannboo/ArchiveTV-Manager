import { useEffect } from "react";
import { Link } from "react-router-dom";
import { api, type Entry, type Rule } from "../api";
import { Chip, fmtBytes, Loading, useLoad, useMeta, useTime } from "../ui";

interface Dash {
  counts: Record<string, number>;
  by_provider: Record<string, number>;
  channels: { id: number; slug: string; name: string; number: number | null; enabled: boolean; now: Entry | null; next: Entry | null;
    eligible: number; horizon: string | null }[];
  events: { at: string; message: string }[];
  providers: { name: string; label: string; configured: boolean; unofficial?: boolean; last_success_at: string | null; last_error: string | null;
    last_error_at: string | null; quota_used?: number; quota_limit?: number }[];
  rules: Rule[];
  unavailable_sources: number;
  failures_24h: number;
  streams: { channel_id: number; name: string; viewers: number; mode: string | null; now_playing: string | null }[];
  catchup: { session_id: string; programme: string }[];
  resources: {
    alerts: string[]; cpu_percent: number | null; memory: { process_rss: number; cgroup_used?: number; cgroup_limit?: number };
    ffmpeg_rss_bytes: number; ffmpeg_processes: number; buffers: { queued_bytes: number; keyframe_cache_bytes: number };
    database_bytes: number; tmp: { percent: number }; minimal_disk: boolean;
  };
  jobs: Record<string, { last_run: string | null; next_run: string | null }>;
}

export default function Dashboard() {
  const meta = useMeta();
  const t = useTime();
  const { data, error, loading, reload } = useLoad(() => api.get<Dash>("/dashboard"), []);
  useEffect(() => {
    const h = setInterval(reload, 15000);
    return () => clearInterval(h);
  }, []);
  if (!data) return <Loading error={error} loading={loading} />;
  const r = data.resources;
  return (
    <>
      <h1>Status</h1>
      <div className="grid">
        <div className="panel"><div className="muted small">Waiting in inbox</div><div className="stat"><Link to="/review">{data.counts.PENDING_REVIEW ?? 0}</Link></div></div>
        <div className="panel"><div className="muted small">Approved</div><div className="stat">{data.counts.APPROVED ?? 0}</div></div>
        <div className="panel"><div className="muted small">Catalogue by provider</div>
          {Object.entries(data.by_provider).map(([p, n]) => <div key={p}>{meta.providers[p] ?? p}: <b>{n}</b></div>)}
          {!Object.keys(data.by_provider).length && <div className="muted">Empty — add a discovery source.</div>}
        </div>
        <div className="panel"><div className="muted small">Active streams</div><div className="stat">{data.streams.length}</div>
          {data.streams.map((s) => <div key={s.channel_id} className="small">{s.name} — {s.viewers} viewer(s), {s.mode}</div>)}
          {!!data.catchup.length && <div className="small"><Link to="/catchup">{data.catchup.length} catch-up session(s)</Link></div>}
        </div>
        <div className="panel"><div className="muted small">Unavailable sources / failures (24h)</div>
          <div className="stat">{data.unavailable_sources} / {data.failures_24h}</div></div>
      </div>

      {r.alerts.map((a) => <div key={a} className="flash error">Resource alert: {a}</div>)}
      <div className="panel small">
        <b>Resources</b> · CPU {r.cpu_percent ?? "—"}% of a core · memory {fmtBytes(r.memory.cgroup_used ?? r.memory.process_rss)}
        {r.memory.cgroup_limit ? ` of ${fmtBytes(r.memory.cgroup_limit)}` : ""} (FFmpeg {fmtBytes(r.ffmpeg_rss_bytes)}, {r.ffmpeg_processes} process
        {r.ffmpeg_processes === 1 ? "" : "es"}) · stream buffers {fmtBytes(r.buffers.queued_bytes + r.buffers.keyframe_cache_bytes)} in RAM ·
        database {fmtBytes(r.database_bytes)} · temp {r.tmp.percent}% ·{" "}
        {r.minimal_disk ? <Chip cls="ok">minimal-disk: no media written</Chip> : <Chip cls="warn">disk cache allowed</Chip>}
      </div>

      <h2>Channels</h2>
      <div className="panel scroll">
        <table>
          <thead><tr><th>#</th><th>Channel</th><th>On now</th><th>Next</th><th>Eligible programmes</th><th>Scheduled until</th></tr></thead>
          <tbody>
            {data.channels.map((c) => (
              <tr key={c.id}>
                <td>{c.number ?? ""}</td>
                <td><Link to={`/channels/${c.id}`}>{c.name}</Link> {!c.enabled && <Chip>disabled</Chip>}<div className="small muted"><code>{c.slug}</code></div></td>
                <td>{c.now ? <>{c.now.title}<div className="small muted">{t(c.now.start, "time").slice(0, 5)}–{t(c.now.end, "time").slice(0, 5)} · {c.now.kind}</div></> : <span className="muted">—</span>}</td>
                <td>{c.next && <>{c.next.title}<div className="small muted">{t(c.next.start, "time").slice(0, 5)}</div></>}</td>
                <td>{c.eligible ? c.eligible : <Chip cls="bad">none — off air</Chip>}</td>
                <td>{t(c.horizon)}</td>
              </tr>
            ))}
            {!data.channels.length && <tr><td colSpan={6} className="muted">No channels yet. <Link to="/channels">Create one</Link>.</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="grid">
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Providers</h3>
          {data.providers.map((p) => (
            <div key={p.name} className="small" style={{ marginBottom: 8 }}>
              <b>{p.label}</b> {!p.configured && <Chip cls="bad">not configured</Chip>}
              {p.unofficial && <Chip cls="warn" title="ytscrape: YouTube's private API, used until an API key is added">unofficial (no key)</Chip>}<br />
              Last success: {t(p.last_success_at)}
              {p.last_error && <><br /><Chip cls="bad">error</Chip> {t(p.last_error_at)} — {p.last_error.slice(0, 160)}</>}
              {p.quota_limit !== undefined && <><br />Quota today: {p.quota_used} / {p.quota_limit} units</>}
            </div>
          ))}
        </div>
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Recent discovery</h3>
          {data.rules.map((r) => (
            <div key={r.id} className="small" style={{ marginBottom: 6 }}>
              {r.name} <span className="muted">({r.provider} · {r.kind})</span><br />{t(r.last_run_at)}{" "}
              {r.last_status === "error" ? <Chip cls="bad">error</Chip> : r.last_status && <Chip cls="ok">{r.last_status}</Chip>} {r.last_new_count} new
            </div>
          ))}
          {!data.rules.length && <div className="muted small">No discovery rules. <Link to="/discovery">Add one</Link>.</div>}
        </div>
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Recent playback errors</h3>
          {data.events.map((e, i) => <div key={i} className="small"><span className="muted">{t(e.at)}</span> {e.message}</div>)}
          {!data.events.length && <div className="muted small">None.</div>}
        </div>
      </div>
      <p className="small muted">
        Background jobs (APScheduler):{" "}
        {Object.entries(data.jobs).map(([k, v]) => `${k} ${v.last_run ? t(v.last_run, "time").slice(0, 5) : "—"}`).join(", ")}. Times in {meta.timezone}.
      </p>
    </>
  );
}
