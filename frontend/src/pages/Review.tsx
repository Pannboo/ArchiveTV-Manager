import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, qs, type AirResult, type Channel, type MediaDetail, type Paged } from "../api";
import { Act, airSummary, Chip, fmtDur, inboxChanged, Loading, Preview, StatusChip, useLoad, useMeta, useToast } from "../ui";

// The fine-grained steps are still here for anyone who wants them.
const MORE_ACTIONS: [string, string][] = [
  ["approve", "Approve only (don't authorise)"], ["authorise", "Authorise rights only"], ["assign", "Add to channel only"],
  ["catalogue_only", "Rights: catalogue only"], ["quarantine", "Quarantine"], ["pending", "Back to inbox"], ["revalidate", "Re-check playback"],
];
const VIEWS: [string, string][] = [["PENDING_REVIEW", "Waiting"], ["REJECTED", "Rejected"], ["QUARANTINED", "Quarantined"], ["UNAVAILABLE", "Unavailable"]];

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
  const [channelId, setChannelId] = useState<string>("");
  const [more, setMore] = useState("");
  const [problems, setProblems] = useState<AirResult[]>([]);
  const set = (k: string, v: string) => { const n = new URLSearchParams(sp); if (v) n.set(k, v); else n.delete(k); if (k !== "page") n.delete("page"); setSp(n); };
  const items = list.data?.items ?? [];
  const done = () => { setSel([]); list.reload(); inboxChanged(); };

  const air = async (ids: number[]) => {
    const r = await api.post<{ on_air: number; not_airing: AirResult[] }>("/media/bulk",
      { ids, action: "air", channel_id: channelId ? Number(channelId) : null });
    setProblems(r.not_airing);
    done();
    const s = airSummary([...Array(r.on_air).fill({ on_air: true, title: "", problem: null }), ...r.not_airing]);
    notify(s.msg, s.ok ? "ok" : "error");
  };
  const reject = async (ids: number[]) => {
    await api.post("/media/bulk", { ids, action: "reject" });
    done();
    notify(`Rejected ${ids.length} item${ids.length === 1 ? "" : "s"}. They won't come back if found again.`);
  };

  return (
    <>
      <h1>Inbox</h1>
      <p className="muted" style={{ marginTop: -8 }}>New videos your <Link to="/discovery">discovery rules</Link> found. <b>Put on air</b> approves
        a video and adds it to a channel in one go; <b>Reject</b> hides it for good. To skip the inbox entirely, paste links on a channel page, or turn on
        “put new finds on air automatically” for a rule.</p>

      <div className="toolbar">
        <span className="tabs">
          {VIEWS.map(([k, v]) => <button key={k} type="button" className={status === k ? "on" : ""} onClick={() => set("status", k)}>{v}</button>)}
        </span>
        <select value={provider} onChange={(e) => set("provider", e.target.value)}><option value="">All sites</option>
          {Object.entries(meta.providers).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        <input type="text" placeholder="Search… (Enter)" defaultValue={q} onKeyDown={(e) => e.key === "Enter" && set("q", (e.target as HTMLInputElement).value)} />
        <span className="small muted">{list.data?.total ?? 0} item(s)</span>
      </div>

      <div className="panel toolbar action-bar">
        <label className="inline"><input type="checkbox" checked={!!items.length && sel.length === items.length}
          onChange={(e) => setSel(e.target.checked ? items.map((i) => i.id) : [])} /> Select all</label>
        <label className="inline">Put on
          <select value={channelId} onChange={(e) => setChannelId(e.target.value)}>
            <option value="">the rule's channel(s)</option>
            {channels.data?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}</select></label>
        <Act className="primary" disabled={!sel.length} run={() => air(sel)}>Put {sel.length || ""} on air</Act>
        <Act className="danger" disabled={!sel.length} run={() => reject(sel)}>Reject {sel.length || ""}</Act>
        <span className="spacer" />
        <select value={more} onChange={(e) => setMore(e.target.value)} title="Individual review steps">
          <option value="">More actions…</option>
          {MORE_ACTIONS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select>
        {more && <Act disabled={!sel.length || (more === "assign" && !channelId)} run={async () => {
          await api.post("/media/bulk", { ids: sel, action: more, channel_id: channelId ? Number(channelId) : null });
          setMore("");
          done();
        }} ok="Done.">{more === "assign" && !channelId ? "Pick a channel first" : "Apply"}</Act>}
      </div>

      {!!problems.length && (
        <div className="flash error">
          <b>{problems.length} item{problems.length === 1 ? "" : "s"} can't air yet:</b>
          <ul className="results">{problems.map((p) => <li key={p.id}><Link to={`/items/${p.id}`}>{p.title}</Link> — {p.problem}</li>)}</ul>
          <button type="button" onClick={() => setProblems([])}>Dismiss</button>
        </div>
      )}

      <Loading error={list.error} loading={list.loading && !list.data} />
      <div className="panel">
        {items.map((it) => (
          <div key={it.id} className="review-card">
            <div><input type="checkbox" checked={sel.includes(it.id)} onChange={(e) => setSel(e.target.checked ? [...sel, it.id] : sel.filter((x) => x !== it.id))} /></div>
            <div className="media"><Preview sources={it.playback_sources} thumb={it.thumbnail_url} /></div>
            <div className="meta">
              <div className="spread"><Link to={`/items/${it.id}`}><b>{it.title}</b></Link>
                {status !== "PENDING_REVIEW" && <StatusChip s={it.review_status} />}</div>
              <div className="small muted">{meta.providers[it.provider] ?? it.provider} · {it.creator ?? "unknown creator"} · {fmtDur(it.duration)}
                {it.publication_date && ` · ${it.publication_date.slice(0, 10)}`}{it.url && <> · <a href={it.url} target="_blank" rel="noreferrer">open original</a></>}</div>
              {it.description && <div className="desc">{it.description}</div>}
              {!!it.flags.length && <ul className="flags">{it.flags.map((f, i) => <li key={i}>{f}</li>)}</ul>}
              {it.duplicate_of_id && <div className="small"><Chip cls="warn">possible duplicate</Chip> of <Link to={`/items/${it.duplicate_of_id}`}>#{it.duplicate_of_id}</Link></div>}
              <div className="row" style={{ marginTop: 10 }}>
                <Act className="primary" run={() => air([it.id])}>Put on air</Act>
                {it.review_status !== "REJECTED" && <Act className="danger" run={() => reject([it.id])}>Reject</Act>}
                <Link className="small" to={`/items/${it.id}`}>Details</Link>
              </div>
            </div>
          </div>
        ))}
        {list.data && !items.length && <p className="muted">{status === "PENDING_REVIEW" ? "All caught up: nothing is waiting." : "Nothing here."}</p>}
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
