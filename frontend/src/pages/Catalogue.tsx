import { Link, useSearchParams } from "react-router-dom";
import { api, qs, type Channel, type MediaSummary, type Paged } from "../api";
import { Chip, EligChip, fmtDur, IptvChip, Loading, StatusChip, useLoad, useMeta, useTime } from "../ui";

export default function Catalogue() {
  const meta = useMeta();
  const t = useTime();
  const [sp, setSp] = useSearchParams();
  const f = { provider: sp.get("provider") ?? "", status: sp.get("status") ?? "", q: sp.get("q") ?? "",
    eligibility: sp.get("eligibility") ?? "", channel_id: sp.get("channel_id") ?? "" };
  const page = Number(sp.get("page") ?? 1);
  const list = useLoad(() => api.get<Paged<MediaSummary>>(`/catalogue${qs({ ...f, page })}`), [sp.toString()]);
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); if (k !== "page") n.delete("page"); setSp(n); };

  return (
    <>
      <h1>Media catalogue</h1>
      <div className="toolbar">
        <input type="text" placeholder="Search title, description, creator, id…" defaultValue={f.q}
          onKeyDown={(e) => e.key === "Enter" && set("q", (e.target as HTMLInputElement).value)} />
        <select value={f.provider} onChange={(e) => set("provider", e.target.value)}><option value="">All providers</option>
          {Object.entries(meta.providers).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <select value={f.status} onChange={(e) => set("status", e.target.value)}><option value="">Any status</option>
          {meta.review_statuses.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ").toLowerCase()}</option>)}</select>
        <select value={f.eligibility} onChange={(e) => set("eligibility", e.target.value)}><option value="">Any playback</option>
          {meta.eligibility.map((s) => <option key={s} value={s}>{s.replace(/_/g, " ").toLowerCase()}</option>)}</select>
        <select value={f.channel_id} onChange={(e) => set("channel_id", e.target.value)}><option value="">Any channel</option>
          {channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
        <span className="small muted">{list.data?.total ?? 0} item(s)</span>
      </div>
      <Loading error={list.error} loading={list.loading && !list.data} />
      <div className="panel scroll">
        <table>
          <thead><tr><th>Title</th><th>Provider</th><th>Duration</th><th>Status</th><th>Playback</th><th>Updated</th></tr></thead>
          <tbody>
            {list.data?.items.map((it) => (
              <tr key={it.id}>
                <td><Link to={`/items/${it.id}`}>{it.title}</Link>{it.duplicate_of_id && <> <Chip cls="warn">possible duplicate</Chip></>}
                  <div className="small muted">{it.creator ?? ""}</div></td>
                <td className="small">{meta.providers[it.provider] ?? it.provider}</td>
                <td className="small">{fmtDur(it.duration)}</td>
                <td><StatusChip s={it.review_status} /></td>
                <td className="row"><EligChip e={it.eligibility} /><IptvChip st={it.iptv_status} /></td>
                <td className="small">{t(it.updated_at)}</td>
              </tr>
            ))}
            {list.data && !list.data.items.length && <tr><td colSpan={6} className="muted">No matching items.</td></tr>}
          </tbody>
        </table>
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
