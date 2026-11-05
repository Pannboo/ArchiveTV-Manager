import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, IPTV_SOURCE_TYPES, type Channel, type MediaDetail, type MediaSummary, type Source } from "../api";
import { Act, Chip, EligChip, Field, fmtBytes, fmtDur, IptvChip, label, Loading, Preview, RightsChip, StatusChip, useLoad, useMeta, useTime } from "../ui";

interface History {
  history: { at: string; actor: string; action: string; from: string | null; to: string | null; note: string | null }[];
  duplicates: MediaSummary[];
  best_source_id: number | null;
}

export default function Item() {
  const id = Number(useParams().id);
  const meta = useMeta();
  const t = useTime();
  const item = useLoad(() => api.get<MediaDetail>(`/media/${id}`), [id]);
  const hist = useLoad(() => api.get<History>(`/media/${id}/history`), [id]);
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const reload = () => { item.reload(); hist.reload(); };
  const it = item.data;
  if (!it) return <Loading error={item.error} loading={item.loading} />;
  const chName = (cid: number) => channels.data?.find((c) => c.id === cid)?.name ?? `#${cid}`;

  return (
    <>
      <div className="spread">
        <h1>{it.title}</h1>
        <span className="row"><StatusChip s={it.review_status} /><EligChip e={it.eligibility} /><IptvChip st={it.iptv_status} /></span>
      </div>
      <div className="two">
        <div>
          <div className="panel"><Preview sources={it.playback_sources} thumb={it.thumbnail_url} />
            <p className="small muted">{meta.providers[it.provider] ?? it.provider} · <code>{it.external_id}</code>
              {it.url && <> · <a href={it.url} target="_blank" rel="noreferrer">open source page</a></>}</p>
            {!!it.flags.length && <ul className="flags">{it.flags.map((f, i) => <li key={i}>{f}</li>)}</ul>}
          </div>
          <MetadataForm it={it} onSaved={(d) => { item.setData(d); hist.reload(); }} />
          <SourcesPanel it={it} bestId={hist.data?.best_source_id ?? null} reload={reload} />
        </div>
        <div>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>Review</h3>
            <div className="row">
              {(["APPROVED", "REJECTED", "QUARANTINED", "PENDING_REVIEW"] as const).map((s) => (
                <Act key={s} className={s === "APPROVED" ? "primary" : s === "REJECTED" ? "danger" : ""} disabled={it.review_status === s}
                  run={async () => { item.setData(await api.post<MediaDetail>(`/media/${id}/status`, { status: s })); hist.reload(); }}
                  ok={`Status set to ${label(s)}.`}>{label(s)}</Act>
              ))}
            </div>
            {it.review_note && <p className="small muted">Note: {it.review_note}</p>}
            <p className="small muted">Approval is separate from rights: a programme airs only when it is approved <i>and</i> one of its
              sources is validated, healthy and has authorised rights.</p>
            <div className="row">
              <Act run={async () => { item.setData(await api.post<MediaDetail>(`/media/${id}/revalidate`)); }} ok="Playback sources revalidated.">Revalidate</Act>
              {it.provider !== "local" && <Act run={async () => { item.setData(await api.post<MediaDetail>(`/media/${id}/refresh`)); }} ok="Provider metadata refreshed.">Refresh from provider</Act>}
            </div>
          </div>
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>Channels</h3>
            {it.channels.map((c) => (
              <div key={`${c.channel_id}-${c.role}`} className="spread small">
                <span><Link to={`/channels/${c.channel_id}`}>{chName(c.channel_id)}</Link> <Chip>{c.role}</Chip></span>
                <Act className="danger" run={async () => { item.setData(await api.del<MediaDetail>(`/media/${id}/channels/${c.channel_id}?role=${c.role}`)); }}>Remove</Act>
              </div>
            ))}
            {!it.channels.length && <p className="small muted">Not on any channel.</p>}
            <AssignForm channels={channels.data ?? []} roles={meta.roles} onAssign={async (cid, role) => {
              item.setData(await api.put<MediaDetail>(`/media/${id}/channels/${cid}`, { role }));
            }} />
          </div>
          {!!hist.data?.duplicates.length && (
            <div className="panel">
              <h3 style={{ marginTop: 0 }}>Possible duplicates</h3>
              {hist.data.duplicates.map((d) => <div key={d.id} className="small"><Link to={`/items/${d.id}`}>#{d.id} {d.title}</Link> <StatusChip s={d.review_status} /></div>)}
            </div>
          )}
          <div className="panel">
            <h3 style={{ marginTop: 0 }}>History</h3>
            {hist.data?.history.map((h, i) => (
              <div key={i} className="small"><span className="muted">{t(h.at)}</span> <b>{h.action.replace(/_/g, " ")}</b>
                {h.from || h.to ? ` ${h.from ?? "—"} → ${h.to ?? "—"}` : ""} {h.note && <span className="muted">— {h.note}</span>} <span className="muted">({h.actor})</span></div>
            ))}
            {hist.data && !hist.data.history.length && <p className="small muted">No history.</p>}
          </div>
        </div>
      </div>
    </>
  );
}

function AssignForm({ channels, roles, onAssign }: { channels: Channel[]; roles: string[]; onAssign: (cid: number, role: string) => Promise<void> }) {
  const [cid, setCid] = useState("");
  const [role, setRole] = useState("programme");
  return (
    <div className="row" style={{ marginTop: 8 }}>
      <select value={cid} onChange={(e) => setCid(e.target.value)}><option value="">Add to channel…</option>
        {channels.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
      <select value={role} onChange={(e) => setRole(e.target.value)}>{roles.map((r) => <option key={r}>{r}</option>)}</select>
      <Act disabled={!cid} run={() => onAssign(Number(cid), role)} ok="Assigned.">Assign</Act>
    </div>
  );
}

function MetadataForm({ it, onSaved }: { it: MediaDetail; onSaved: (d: MediaDetail) => void }) {
  const init = () => ({ title: it.title, description: it.description ?? "", publication_date: it.publication_date ?? "", series: it.series ?? "",
    season: it.season?.toString() ?? "", episode: it.episode?.toString() ?? "", duration: it.duration ? String(Math.round(it.duration)) : "",
    tags: it.tags.join(", "), categories: it.categories.join(", ") });
  const [f, setF] = useState(init);
  useEffect(() => setF(init()), [it.id, it.updated_at]);
  const num = (v: string) => (v.trim() ? Number(v) : null);
  const list = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);
  const bind = (k: keyof typeof f) => ({ value: f[k], onChange: (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value }) });
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Metadata</h3>
      <div className="fields">
        <Field label="Title"><input type="text" {...bind("title")} /></Field>
        <Field label="Series"><input type="text" {...bind("series")} /></Field>
        <Field label="Season"><input type="number" min={0} {...bind("season")} /></Field>
        <Field label="Episode"><input type="number" min={0} {...bind("episode")} /></Field>
        <Field label="Published"><input type="text" placeholder="YYYY-MM-DD" {...bind("publication_date")} /></Field>
        <Field label={`Duration (seconds) · ${fmtDur(it.duration)}`}><input type="number" min={1} {...bind("duration")} /></Field>
        <Field label="Tags (comma)"><input type="text" {...bind("tags")} /></Field>
        <Field label="Categories (comma)"><input type="text" {...bind("categories")} /></Field>
      </div>
      <Field label="Description"><textarea {...bind("description")} /></Field>
      <p className="row"><Act className="primary" run={async () => onSaved(await api.patch<MediaDetail>(`/media/${it.id}`, {
        title: f.title, description: f.description || null, publication_date: f.publication_date || null, series: f.series || null,
        season: num(f.season), episode: num(f.episode), duration: num(f.duration), tags: list(f.tags), categories: list(f.categories),
      }))} ok="Metadata saved (locked against provider refresh).">Save metadata</Act>
        <span className="small muted">Saved edits are locked against provider refreshes.</span></p>
    </div>
  );
}

function SourcesPanel({ it, bestId, reload }: { it: MediaDetail; bestId: number | null; reload: () => void }) {
  const meta = useMeta();
  const t = useTime();
  const [notes, setNotes] = useState<Record<number, string>>({});
  const files = useLoad(() => api.get<{ path: string; imported: boolean }[]>("/local/files"), []);
  const [add, setAdd] = useState({ playback_type: "direct_url", url: "", local_path: "", label: "", rights_note: "", authorise: false });
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Playback sources</h3>
      <div className="scroll">
        <table>
          <thead><tr><th>Source</th><th>Technical</th><th>Rights</th><th></th></tr></thead>
          <tbody>
            {it.playback_sources.map((ps: Source) => (
              <tr key={ps.id}>
                <td className="small"><Chip>{ps.type}</Chip>{ps.id === bestId && <> <Chip cls="ok">airs from this</Chip></>}<br />
                  <span style={{ wordBreak: "break-all" }}>{ps.label ?? ps.url ?? ps.local_path}</span></td>
                <td className="small"><EligChip e={ps.eligibility} /> {ps.health_ok ? <Chip cls="ok">healthy</Chip> : <Chip cls="bad">failing</Chip>}
                  {ps.live ? <> <Chip cls="bad">● live</Chip></> : ps.seekable === false && <> <Chip cls="warn">no seek</Chip></>}
                  <div className="muted">{ps.video_codec ?? "?"}/{ps.audio_codec ?? "—"} {ps.width ? `${ps.width}×${ps.height}` : ""} · {fmtDur(ps.duration)}
                    {ps.size_bytes ? ` · ${fmtBytes(ps.size_bytes)}` : ""}</div>
                  <div className="muted">checked {t(ps.last_validated_at)}{ps.last_error && ` — ${ps.last_error.slice(0, 140)}`}</div></td>
                <td className="small"><RightsChip r={ps.rights_status} />{ps.rights_note && <div className="muted">{ps.rights_note}</div>}
                  {IPTV_SOURCE_TYPES.includes(ps.type) && (
                    <div className="row" style={{ marginTop: 4 }}>
                      <input type="text" placeholder="note" value={notes[ps.id] ?? ""} onChange={(e) => setNotes({ ...notes, [ps.id]: e.target.value })} style={{ width: 110 }} />
                      {meta.rights.filter((r) => r !== ps.rights_status).map((r) => (
                        <Act key={r} run={async () => { await api.post(`/sources/${ps.id}/rights`, { rights: r, note: notes[ps.id] || null }); reload(); }}
                          ok={`Rights set to ${label(r)}.`}>{label(r)}</Act>
                      ))}
                    </div>
                  )}</td>
                <td className="row">
                  {IPTV_SOURCE_TYPES.includes(ps.type) && <Act run={async () => {
                    const r = await api.post<{ ok: boolean; message: string | null }>(`/sources/${ps.id}/validate`);
                    reload();
                    if (!r.ok) throw new Error(`Validation: ${r.message ?? "failed"}`);
                  }} ok="Validated.">Validate</Act>}
                  <Act className="danger" confirm="Remove this playback source?" run={async () => { await api.del(`/sources/${ps.id}`); reload(); }} ok="Source removed.">Remove</Act>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <h3>Add an authorised source</h3>
      <p className="small muted">For example a rights-holder MP4 for a programme found on YouTube. YouTube URLs are never accepted as playback sources.</p>
      <div className="fields">
        <Field label="Type"><select value={add.playback_type} onChange={(e) => setAdd({ ...add, playback_type: e.target.value })}>
          <option value="direct_url">Direct media URL (MP4 / TS / HLS)</option><option value="local_file">Local file in the media folder</option></select></Field>
        {add.playback_type === "direct_url"
          ? <Field label="URL"><input type="url" value={add.url} onChange={(e) => setAdd({ ...add, url: e.target.value })} /></Field>
          : <Field label="File"><select value={add.local_path} onChange={(e) => setAdd({ ...add, local_path: e.target.value })}><option value="">—</option>
              {files.data?.map((f) => <option key={f.path} value={f.path}>{f.path}</option>)}</select></Field>}
        <Field label="Label"><input type="text" value={add.label} onChange={(e) => setAdd({ ...add, label: e.target.value })} /></Field>
        <Field label="Rights note"><input type="text" value={add.rights_note} onChange={(e) => setAdd({ ...add, rights_note: e.target.value })} /></Field>
      </div>
      <label className="inline"><input type="checkbox" checked={add.authorise} onChange={(e) => setAdd({ ...add, authorise: e.target.checked })} /> Authorise rights now (if it validates)</label>
      <p><Act className="primary" run={async () => {
        await api.post(`/media/${it.id}/sources`, { ...add, url: add.url || null, local_path: add.local_path || null, label: add.label || null,
          rights_note: add.rights_note || null });
        setAdd({ ...add, url: "", local_path: "", label: "", rights_note: "" });
        reload();
      }} ok="Source added and validated.">Add source</Act></p>
    </div>
  );
}
