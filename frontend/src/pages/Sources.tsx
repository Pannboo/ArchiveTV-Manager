import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, IPTV_SOURCE_TYPES, qs, type Channel, type Source } from "../api";
import { Act, Chip, EligChip, fmtDur, label, Loading, RightsChip, useLoad, useMeta, useTime } from "../ui";

export default function Sources() {
  const meta = useMeta();
  const t = useTime();
  const [sp, setSp] = useSearchParams();
  const f = { rights: sp.get("rights") ?? "", eligibility: sp.get("eligibility") ?? "", type: sp.get("type") ?? "", unhealthy: sp.get("unhealthy") ?? "" };
  const list = useLoad(() => api.get<Source[]>(`/sources${qs({ ...f, unhealthy: f.unhealthy ? true : undefined })}`), [sp.toString()]);
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); setSp(n); };

  return (
    <>
      <h1>Playback sources</h1>
      <p className="small muted">Every playable file or stream behind the catalogue. Technical eligibility (validation, health) and rights are separate:
        a source feeds IPTV only when it is IPTV-ready, healthy and authorised.</p>
      <div className="toolbar">
        <select value={f.rights} onChange={(e) => set("rights", e.target.value)}><option value="">Any rights</option>
          {meta.rights.map((r) => <option key={r} value={r}>{label(r)}</option>)}</select>
        <select value={f.eligibility} onChange={(e) => set("eligibility", e.target.value)}><option value="">Any eligibility</option>
          {meta.eligibility.map((r) => <option key={r} value={r}>{label(r)}</option>)}</select>
        <select value={f.type} onChange={(e) => set("type", e.target.value)}><option value="">Any type</option>
          {meta.playback_types.map((r) => <option key={r} value={r}>{r}</option>)}</select>
        <label className="inline"><input type="checkbox" checked={!!f.unhealthy} onChange={(e) => set("unhealthy", e.target.checked ? "1" : "")} /> Failing only</label>
        <span className="small muted">{list.data?.length ?? 0} source(s)</span>
      </div>
      <Loading error={list.error} loading={list.loading && !list.data} />
      <div className="panel scroll">
        <table>
          <thead><tr><th>Item</th><th>Source</th><th>Technical</th><th>Rights</th><th>Priority</th><th></th></tr></thead>
          <tbody>
            {list.data?.map((ps) => (
              <tr key={ps.id}>
                <td className="small"><Link to={`/items/${ps.media_item_id}`}>{ps.item_title}</Link></td>
                <td className="small"><Chip>{ps.type}</Chip><div style={{ wordBreak: "break-all" }}>{ps.label ?? ps.url ?? ps.local_path}</div></td>
                <td className="small"><EligChip e={ps.eligibility} /> {ps.health_ok ? <Chip cls="ok">healthy</Chip> : <Chip cls="bad">failing</Chip>}
                  <div className="muted">{ps.video_codec ?? "?"}/{ps.audio_codec ?? "—"} · {fmtDur(ps.duration)} · checked {t(ps.last_validated_at)}</div>
                  {ps.last_error && <div className="muted">{ps.last_error.slice(0, 120)}</div>}</td>
                <td className="small"><RightsChip r={ps.rights_status} /></td>
                <td><input type="number" defaultValue={ps.priority} style={{ width: 70 }} onBlur={async (e) => {
                  if (Number(e.target.value) !== ps.priority) await api.patch(`/sources/${ps.id}`, { priority: Number(e.target.value) });
                }} /></td>
                <td className="row">
                  {IPTV_SOURCE_TYPES.includes(ps.type) && <Act run={async () => {
                    const r = await api.post<{ ok: boolean; message: string | null }>(`/sources/${ps.id}/validate`);
                    list.reload();
                    if (!r.ok) throw new Error(`Validation: ${r.message ?? "failed"}`);
                  }} ok="Validated.">Validate</Act>}
                  <Act className="danger" confirm="Remove this playback source?" run={async () => { await api.del(`/sources/${ps.id}`); list.reload(); }}>Remove</Act>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <LocalImport onDone={list.reload} />
    </>
  );
}

function LocalImport({ onDone }: { onDone: () => void }) {
  const meta = useMeta();
  const files = useLoad(() => api.get<{ path: string; imported: boolean }[]>("/local/files"), []);
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const [pick, setPick] = useState<string[]>([]);
  const [o, setO] = useState({ approve: false, authorise: false, channel_id: "", role: "ident" });
  const fresh = files.data?.filter((f) => !f.imported) ?? [];
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Import local files</h3>
      <p className="small muted">Files in the media folder (idents, filler, rights-holder masters). They are checksummed for duplicate detection and validated.</p>
      <div style={{ maxHeight: 220, overflow: "auto" }}>
        {fresh.map((f) => (
          <label key={f.path} className="small" style={{ display: "flex", gap: 6 }}>
            <input type="checkbox" checked={pick.includes(f.path)} onChange={(e) => setPick(e.target.checked ? [...pick, f.path] : pick.filter((x) => x !== f.path))} />
            <code>{f.path}</code>
          </label>
        ))}
        {files.data && !fresh.length && <p className="small muted">No new files in the media folder.</p>}
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <label className="inline"><input type="checkbox" checked={o.approve} onChange={(e) => setO({ ...o, approve: e.target.checked })} /> Approve</label>
        <label className="inline"><input type="checkbox" checked={o.authorise} onChange={(e) => setO({ ...o, authorise: e.target.checked })} /> Authorise rights (I hold them)</label>
        <select value={o.channel_id} onChange={(e) => setO({ ...o, channel_id: e.target.value })}><option value="">— add to channel —</option>
          {channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
        <select value={o.role} onChange={(e) => setO({ ...o, role: e.target.value })}>{meta.roles.map((r) => <option key={r}>{r}</option>)}</select>
        <Act className="primary" disabled={!pick.length} run={async () => {
          const r = await api.post<{ imported: number[] }>("/local/import", { paths: pick, ...o, channel_id: o.channel_id ? Number(o.channel_id) : null });
          setPick([]); files.reload(); onDone();
          return r;
        }} ok={(r) => `Imported ${(r as { imported: number[] }).imported.length} local file(s).`}>Import {pick.length} selected</Act>
      </div>
    </div>
  );
}
