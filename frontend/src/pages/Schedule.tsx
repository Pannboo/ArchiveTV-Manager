import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, qs, type Channel, type Entry } from "../api";
import { Act, Chip, fmtDur, Loading, useLoad, useMeta, useTime } from "../ui";

interface Day {
  day: string; entries: Entry[]; problems: string[]; current_id: number | null; catchup_enabled: boolean;
  eligible: { id: number; title: string; duration: number | null }[]; days: string[]; now: string;
}
interface Version {
  id: number; created_at: string; updated_at: string; reason: string; actor: string; changed_from: string | null;
  horizon: string | null; entries_added: number; entries_removed: number; note: string | null;
}

export default function Schedule() {
  const meta = useMeta();
  const t = useTime();
  const [sp, setSp] = useSearchParams();
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const cid = Number(sp.get("channel") ?? channels.data?.[0]?.id ?? 0);
  const day = sp.get("day") ?? "";
  const ch = channels.data?.find((c) => c.id === cid);
  const sched = useLoad(() => (cid ? api.get<Day>(`/channels/${cid}/schedule/day${qs({ day })}`) : Promise.resolve(null)), [cid, day]);
  const versions = useLoad(() => (cid ? api.get<Version[]>(`/channels/${cid}/schedule/versions?limit=20`) : Promise.resolve([])), [cid]);
  const [pick, setPick] = useState<Record<number, string>>({});
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); n.set(k, v); if (k === "channel") n.delete("day"); setSp(n); };
  const reload = () => { sched.reload(); versions.reload(); };

  if (channels.data && !channels.data.length) return <><h1>Schedule</h1><p className="muted">Create a channel first.</p></>;
  const d = sched.data;
  return (
    <>
      <h1>Schedule</h1>
      <div className="toolbar">
        <select value={cid} onChange={(e) => set("channel", e.target.value)}>{channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
        <select value={d?.day ?? ""} onChange={(e) => set("day", e.target.value)}>
          {d?.days.map((x) => <option key={x} value={x}>{t(`${x}T12:00:00Z`, "date")}{x < d.now.slice(0, 10) ? " (history)" : ""}</option>)}</select>
        <Act run={async () => { await api.post("/schedule/maintain"); reload(); }} ok="Schedules checked and extended.">Check &amp; extend now</Act>
        <Act confirm="Rebuild all upcoming entries for this channel?" run={async () => {
          const r = await api.post<{ added: number }>(`/channels/${cid}/schedule/generate?mode=regenerate`); reload(); return r;
        }} ok={(r) => `Regenerated (${(r as { added: number }).added} entries).`}>Regenerate</Act>
        <span className="small muted">Times in {meta.timezone} · stored in UTC · {d?.eligible.length ?? 0} eligible programme(s)</span>
      </div>
      <Loading error={sched.error} loading={sched.loading && !d} />
      {d && !!d.problems.length && <div className="flash error">Continuity problems: {d.problems.join("; ")}</div>}
      {d && (
        <div className="panel scroll">
          <table>
            <thead><tr><th>Start</th><th>End</th><th>Programme</th><th>Kind</th><th>Block</th><th>Override / replay</th></tr></thead>
            <tbody>
              {d.entries.map((e) => (
                <tr key={e.id} className={e.id === d.current_id ? "now" : ""}>
                  <td>{t(e.start, "time")}</td><td>{t(e.end, "time")}</td>
                  <td>{e.media_item_id ? <Link to={`/items/${e.media_item_id}`}>{e.title}</Link> : e.title}{e.overridden && <> <Chip>override</Chip></>}</td>
                  <td><Chip cls={e.kind === "slate" ? "bad" : undefined}>{e.kind}</Chip></td>
                  <td className="small">{e.block ?? ""}</td>
                  <td>
                    {e.start > d.now && d.eligible.length ? (
                      <span className="row">
                        <select value={pick[e.id] ?? ""} onChange={(x) => setPick({ ...pick, [e.id]: x.target.value })}><option value="">Replace with…</option>
                          {d.eligible.map((it) => <option key={it.id} value={it.id}>{it.title.slice(0, 50)} ({fmtDur(it.duration)})</option>)}</select>
                        <Act disabled={!pick[e.id]} run={async () => {
                          await api.post(`/schedule/entries/${e.id}/override`, { item_id: Number(pick[e.id]) }); reload();
                        }} ok="Programme overridden; later entries were rebuilt.">Replace</Act>
                      </span>
                    ) : e.end <= d.now && d.catchup_enabled && e.kind !== "slate" && ch ? (
                      <a className="small" href={`/catchup/${ch.slug}.ts?start=${encodeURIComponent(e.start)}${meta.feed_query ? `&${meta.feed_query}` : ""}`} title="Open in VLC or another IPTV player">catch-up stream</a>
                    ) : null}
                  </td>
                </tr>
              ))}
              {!d.entries.length && <tr><td colSpan={6} className="muted">Nothing scheduled for this day.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
      <h2>Publication history</h2>
      <div className="panel scroll">
        <table>
          <thead><tr><th>When</th><th>Change</th><th>By</th><th>From</th><th>Entries</th><th>Scheduled until</th><th>Note</th></tr></thead>
          <tbody>
            {versions.data?.map((v) => (
              <tr key={v.id}>
                <td>{t(v.created_at)}{v.updated_at !== v.created_at && <div className="small muted">to {t(v.updated_at, "time").slice(0, 5)}</div>}</td>
                <td><Chip cls={v.reason === "failover" || v.reason === "rebuild" ? "warn" : undefined}>{v.reason}</Chip></td>
                <td className="small">{v.actor}</td><td className="small">{t(v.changed_from)}</td>
                <td className="small">+{v.entries_added} / −{v.entries_removed}</td><td className="small">{t(v.horizon)}</td>
                <td className="small muted">{v.note?.slice(0, 120)}</td>
              </tr>
            ))}
            {versions.data && !versions.data.length && <tr><td colSpan={7} className="muted">No changes recorded yet.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
