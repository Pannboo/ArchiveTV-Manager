import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, type Channel, type Entry } from "../api";
import { Act, Chip, fmtBytes, Loading, useLoad, useMeta, useTime, useToast } from "../ui";

type Prog = Entry & { channel_id: number; channel: string; available: boolean; reason: string | null };
interface Sessions {
  active: { session_id: string; slug: string; programme: string; protocol: string; requested_start: string; position: string; client: string | null }[];
  history: { session_id: string; title: string; requested_start: string; protocol: string; client: string | null; started_at: string;
    status: string; bytes_out: number; error: string | null }[];
}

export default function Catchup() {
  const meta = useMeta();
  const t = useTime();
  const notify = useToast();
  const [sp, setSp] = useSearchParams();
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const cid = Number(sp.get("channel") ?? channels.data?.[0]?.id ?? 0);
  const ch = channels.data?.find((c) => c.id === cid);
  const progs = useLoad(() => (cid ? api.get<Prog[]>(`/catchup/programmes?channel_id=${cid}&limit=150`) : Promise.resolve([])), [cid]);
  const sessions = useLoad(() => api.get<Sessions>("/catchup/sessions?limit=25"), []);
  const [at, setAt] = useState("");
  const tok = meta.feed_query ? `&${meta.feed_query}` : "";

  if (channels.data && !channels.data.length) return <><h1>Catch-up</h1><p className="muted">Create a channel first.</p></>;
  return (
    <>
      <h1>Catch-up</h1>
      <p className="small muted">Past programmes are replayed from their original source, each viewer with an independent playhead. Nothing is
        recorded; availability is checked again at playback time. Times in {meta.timezone}.</p>
      <div className="toolbar">
        <select value={cid} onChange={(e) => setSp({ channel: e.target.value })}>{channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
      </div>
      {ch && (
        <div className="grid">
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>{ch.name}</h3>
            <p className="small">Catch-up: {ch.catchup_enabled ? <Chip cls="ok">enabled</Chip> : <Chip cls="bad">disabled</Chip>} · history kept {ch.catchup_days} day
              {ch.catchup_days > 1 ? "s" : ""} · <Link to={`/channels/${ch.id}`}>change</Link></p>
            <div className="row">
              <input type="datetime-local" step={1} value={at} onChange={(e) => setAt(e.target.value)} />
              <Act disabled={!at} run={async () => {
                const r = await api.post<{ ok: boolean; message?: string; title?: string; offset_seconds?: number; programme_start?: string }>(
                  "/catchup/test", { channel_id: cid, at });
                if (!r.ok) throw new Error(`Not playable: ${r.message}`);
                notify(`OK: ${Math.round(r.offset_seconds ?? 0)}s into '${r.title}' (aired ${t(r.programme_start, "time").slice(0, 5)}); its source answers and can seek there.`);
              }}>Test historical seek</Act>
            </div>
            <p className="small muted">Start Over (programme on air now): <code>/startover/{ch.slug}.ts</code></p>
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>Active sessions</h3>
            {sessions.data?.active.map((a) => (
              <div key={a.session_id} className="spread small">
                <span><b>{a.programme}</b> on {a.slug} · {a.protocol} · from {t(a.requested_start, "time")} · now at {t(a.position, "time")}</span>
                <Act className="danger" run={async () => { await api.del(`/catchup/sessions/${a.session_id}`); sessions.reload(); }} ok="Session stopped.">Stop</Act>
              </div>
            ))}
            {sessions.data && !sessions.data.active.length && <p className="small muted">None.</p>}
            <p><button onClick={sessions.reload}>Refresh</button></p>
          </div>
        </div>
      )}
      <h2>Past programmes</h2>
      <Loading error={progs.error} loading={progs.loading && !progs.data} />
      <div className="panel scroll">
        <table>
          <thead><tr><th>Aired</th><th>Programme</th><th>Kind</th><th>Catch-up</th></tr></thead>
          <tbody>
            {progs.data?.map((p) => (
              <tr key={p.id}>
                <td>{t(p.start)}–{t(p.end, "time").slice(0, 5)}</td>
                <td>{p.media_item_id ? <Link to={`/items/${p.media_item_id}`}>{p.title}</Link> : p.title}</td>
                <td><Chip>{p.kind}</Chip></td>
                <td>{p.available
                  ? <><Chip cls="ok">available</Chip> <a className="small" href={`/catchup/${p.channel}.ts?start=${encodeURIComponent(p.start)}${tok}`}>stream</a></>
                  : <><Chip cls="bad">unavailable</Chip> <span className="small muted">{p.reason}</span></>}</td>
              </tr>
            ))}
            {progs.data && !progs.data.length && <tr><td colSpan={4} className="muted">No programme history yet.</td></tr>}
          </tbody>
        </table>
      </div>
      <h2>Recent sessions</h2>
      <div className="panel scroll">
        <table>
          <thead><tr><th>Started</th><th>Programme</th><th>Requested</th><th>Via</th><th>Status</th><th>Sent</th><th>Client</th></tr></thead>
          <tbody>
            {sessions.data?.history.map((r) => (
              <tr key={r.session_id}>
                <td>{t(r.started_at, "full")}</td><td>{r.title}</td><td>{t(r.requested_start, "full")}</td><td>{r.protocol}</td>
                <td><Chip cls={r.status === "failed" ? "bad" : r.status === "active" ? "warn" : "ok"}>{r.status}</Chip>
                  {r.error && <div className="small muted">{r.error.slice(0, 120)}</div>}</td>
                <td className="small">{fmtBytes(r.bytes_out)}</td><td className="small muted">{(r.client ?? "").slice(0, 40)}</td>
              </tr>
            ))}
            {sessions.data && !sessions.data.history.length && <tr><td colSpan={7} className="muted">No catch-up sessions yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
