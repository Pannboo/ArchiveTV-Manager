import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, IPTV_SOURCE_TYPES, qs, type Channel, type MediaDetail, type Paged } from "../api";
import { Act, Chip, EligChip, fmtDur, IptvChip, Loading, Preview, RightsChip, StatusChip, useLoad, useMeta, useToast } from "../ui";

const ACTIONS: [string, string][] = [
  ["approve", "Approve"], ["reject", "Reject"], ["quarantine", "Quarantine"], ["pending", "Back to pending"],
  ["assign", "Assign to channel"], ["authorise", "Authorise rights (validated sources)"],
  ["catalogue_only", "Rights: catalogue only"], ["revalidate", "Revalidate"],
];

export default function Review() {
  const meta = useMeta();
  const notify = useToast();
  const [sp, setSp] = useSearchParams();
  const status = sp.get("status") ?? "PENDING_REVIEW";
  const provider = sp.get("provider") ?? "";
  const q = sp.get("q") ?? "";
  const page = Number(sp.get("page") ?? 1);
  const list = useLoad(() => api.get<Paged<MediaDetail>>(`/catalogue${qs({ status, provider, q, page, per_page: 20, sort: "created", detail: true })}`),
    [status, provider, q, page]);
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const [sel, setSel] = useState<number[]>([]);
  const [action, setAction] = useState("approve");
  const [channelId, setChannelId] = useState<string>("");
  const [note, setNote] = useState("");
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); if (k !== "page") n.delete("page"); setSp(n); };
  const items = list.data?.items ?? [];

  return (
    <>
      <h1>Review queue</h1>
      <div className="toolbar">
        <select value={status} onChange={(e) => set("status", e.target.value)}>
          {meta.review_statuses.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ").toLowerCase()}</option>)}</select>
        <select value={provider} onChange={(e) => set("provider", e.target.value)}><option value="">All providers</option>
          {Object.entries(meta.providers).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <input type="text" placeholder="Search…" defaultValue={q} onKeyDown={(e) => e.key === "Enter" && set("q", (e.target as HTMLInputElement).value)} />
        <span className="small muted">{list.data?.total ?? 0} item(s)</span>
      </div>

      <div className="panel toolbar" style={{ position: "sticky", top: 0, zIndex: 5 }}>
        <label className="inline"><input type="checkbox" checked={!!items.length && sel.length === items.length}
          onChange={(e) => setSel(e.target.checked ? items.map((i) => i.id) : [])} /> All on page</label>
        <select value={action} onChange={(e) => setAction(e.target.value)}>{ACTIONS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <select value={channelId} onChange={(e) => setChannelId(e.target.value)}><option value="">— channel (optional) —</option>
          {channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
        <input type="text" placeholder="Note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
        <Act className="primary" disabled={!sel.length} run={async () => {
          const r = await api.post<{ count: number; skipped_no_iptv_source: number[] }>("/media/bulk",
            { ids: sel, action, note: note || null, channel_id: channelId ? Number(channelId) : null });
          notify(`${action.replace(/_/g, " ")}: ${r.count} item(s)${r.skipped_no_iptv_source.length ? `; ${r.skipped_no_iptv_source.length} had no validated IPTV-capable source` : ""}.`);
          setSel([]);
          list.reload();
        }}>Apply to {sel.length} selected</Act>
      </div>

      <Loading error={list.error} loading={list.loading && !list.data} />
      <div className="panel">
        {items.map((it) => (
          <div key={it.id} className="review-card">
            <div><input type="checkbox" checked={sel.includes(it.id)} onChange={(e) => setSel(e.target.checked ? [...sel, it.id] : sel.filter((x) => x !== it.id))} /></div>
            <div className="media"><Preview sources={it.playback_sources} thumb={it.thumbnail_url} /></div>
            <div className="meta">
              <div className="spread"><Link to={`/items/${it.id}`}><b>{it.title}</b></Link>
                <span className="row"><StatusChip s={it.review_status} /><IptvChip st={it.iptv_status} /></span></div>
              <div className="small muted">{meta.providers[it.provider] ?? it.provider} · {it.creator ?? "unknown creator"} · {fmtDur(it.duration)}
                {it.publication_date && ` · ${it.publication_date.slice(0, 10)}`} · {it.url && <a href={it.url} target="_blank" rel="noreferrer">source</a>}</div>
              {it.description && <div className="desc">{it.description}</div>}
              {!!it.flags.length && <ul className="flags">{it.flags.map((f, i) => <li key={i}>{f}</li>)}</ul>}
              <div className="row small">
                {it.playback_sources.map((ps) => (
                  <span key={ps.id} className="row" title={ps.url ?? ps.local_path ?? ""}>
                    <Chip>{ps.type}</Chip><EligChip e={ps.eligibility} />{IPTV_SOURCE_TYPES.includes(ps.type) ? <RightsChip r={ps.rights_status} /> : null}
                    {ps.video_codec && <span className="muted">{ps.video_codec}/{ps.audio_codec ?? "—"} {ps.width ? `${ps.width}×${ps.height}` : ""}</span>}
                  </span>
                ))}
              </div>
              {it.duplicate_of_id && <div className="small">Possible duplicate of <Link to={`/items/${it.duplicate_of_id}`}>#{it.duplicate_of_id}</Link></div>}
            </div>
          </div>
        ))}
        {list.data && !items.length && <p className="muted">Nothing here.</p>}
      </div>
      {list.data && list.data.pages > 1 && (
        <div className="pager">
          <button disabled={page <= 1} onClick={() => set("page", String(page - 1))}>Previous</button>
          <span className="small muted">Page {page} of {list.data.pages}</span>
          <button disabled={page >= list.data.pages} onClick={() => set("page", String(page + 1))}>Next</button>
        </div>
      )}
    </>
  );
}
