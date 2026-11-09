import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Channel, type LinkResult, type Rule } from "../api";
import { Act, airSummary, Chip, Field, inboxChanged, Loading, useLoad, useMeta, useRemembered, useTime, useToast } from "../ui";

// [kind, label, providers it applies to]
const KINDS: [string, string, string[]][] = [
  ["search", "Keyword search", ["archive", "youtube"]], ["collection", "Archive collection", ["archive"]],
  ["channel", "YouTube channel uploads", ["youtube"]], ["playlist", "YouTube playlist", ["youtube"]],
  ["video", "Single item / URL", ["archive", "youtube"]],
];
// [param, label, placeholder, providers it applies to]
const TEXT_PARAMS: [string, string, string | undefined, string[]][] = [
  ["collection", "Collection(s), ; separated", "classic_tv", ["archive"]], ["creator", "Creator", undefined, ["archive"]],
  ["subject", "Subject", undefined, ["archive"]], ["year_from", "Year from", undefined, ["archive"]], ["year_to", "Year to", undefined, ["archive"]],
  ["channel", "Channel (@handle, URL or UC id)", undefined, ["youtube"]], ["playlist", "Playlist (URL or id)", undefined, ["youtube"]],
  ["published_after", "Published after (YYYY-MM-DD)", undefined, ["youtube"]],
  ["published_before", "Published before (YYYY-MM-DD)", undefined, ["youtube"]],
  ["categories", "Categories (ids or names)", "1, 24", ["youtube"]],
  ["excluded_channels", "Excluded channels/creators (comma)", undefined, ["archive", "youtube"]],
  ["excluded_keywords", "Excluded keywords (comma)", undefined, ["archive", "youtube"]],
  ["sort", "Sort", "addeddate desc", ["archive"]], ["preferred_formats", "Preferred formats, best first", "h.264, mpeg4", ["archive"]],
];
const FLAGS: [string, string, string[]][] = [
  ["licensed_only", "Only items with a declared licence", ["archive"]], ["embeddable_only", "Embeddable only", ["youtube"]],
  ["skip_validation", "Skip the playback check (FFprobe): unchecked videos can't air", ["archive", "youtube"]],
];

type Draft = { id?: number; name: string; provider: string; kind: string; query: string; params: Record<string, unknown>;
  scan_interval_minutes: number; max_results: number; enabled: boolean; channel_ids: number[] };

const empty = (): Draft => ({ name: "", provider: "archive", kind: "search", query: "", params: {}, scan_interval_minutes: 1440,
  max_results: 50, enabled: true, channel_ids: [] });

export default function Discovery() {
  const meta = useMeta();
  const t = useTime();
  const notify = useToast();
  const nav = useNavigate();
  const rules = useLoad(() => api.get<Rule[]>("/discovery/rules"), []);
  const channels = useLoad(() => api.get<Channel[]>("/channels"), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [url, setUrl] = useState("");
  const [target, setTarget] = useState("");
  const [allowed, setAllowed] = useRemembered("atv:permission-confirmed", false);

  const save = async () => {
    if (!draft) return;
    const p = { ...draft.params };
    for (const k of ["min_duration", "max_duration"]) if (p[k] !== undefined && p[k] !== "") p[k] = Number(p[k]) * 60; else delete p[k];
    for (const [k, v] of Object.entries(p)) if (v === "" || v === false) delete p[k];
    const body = { name: draft.name, query: draft.query || null, params: p, scan_interval_minutes: draft.scan_interval_minutes,
      max_results: draft.max_results, enabled: draft.enabled, channel_ids: draft.channel_ids };
    if (draft.id) await api.patch(`/discovery/rules/${draft.id}`, body);
    else await api.post("/discovery/rules", { ...body, provider: draft.provider, kind: draft.kind });
    setDraft(null);
    rules.reload();
  };
  const edit = (r: Rule) => {
    const params: Record<string, unknown> = { ...r.params };
    for (const k of ["min_duration", "max_duration"]) if (typeof params[k] === "number") params[k] = Math.round((params[k] as number) / 6) / 10;
    setDraft({ id: r.id, name: r.name, provider: r.provider, kind: r.kind, query: r.query ?? "", params,
      scan_interval_minutes: r.scan_interval_minutes, max_results: r.max_results, enabled: r.enabled, channel_ids: r.channel_ids });
  };
  const setP = (k: string, v: unknown) => draft && setDraft({ ...draft, params: { ...draft.params, [k]: v } });

  return (
    <>
      <h1>Find videos</h1>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Add one link</h3>
        <p className="small muted" style={{ marginTop: 0 }}>The quickest way to fill a channel is to paste links on the channel's own page. Here you can
          also send a link to the inbox to look at first.</p>
        <div className="row">
          <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} style={{ flex: 1, minWidth: 260 }}
            placeholder="https://archive.org/details/…, a YouTube video, playlist, @channel, or @channel/live URL" />
          <select value={target} onChange={(e) => setTarget(e.target.value)}>
            <option value="">Send to inbox</option>
            {channels.data?.map((c) => <option key={c.id} value={c.id}>Put on air on {c.name}</option>)}</select>
          <Act className="primary" disabled={!url.trim() || (!!target && !allowed)} run={async () => {
            if (target) {
              const r = await api.post<{ results: LinkResult[] }>(`/channels/${target}/links`, { urls: [url] });
              const res = r.results[0];
              if (res?.error) throw new Error(res.error);
              const s = airSummary(res?.items ?? []);
              notify(s.msg, s.ok ? "ok" : "error");
              setUrl("");
              return;
            }
            const r = await api.post<{ new: number; skipped_known: number; errors: string[] }>("/discovery/import", { url });
            if (r.errors.length) throw new Error(`Import failed: ${r.errors.join("; ")}`);
            notify(r.new ? `Added ${r.new} item(s) to the inbox.` : `Nothing new (${r.skipped_known} already in your library).`);
            setUrl("");
            inboxChanged();
            if (r.new) nav("/review");
          }}>Add</Act>
        </div>
        {!!target && <label className="inline small" style={{ marginTop: 8 }}><input type="checkbox" checked={allowed}
          onChange={(e) => setAllowed(e.target.checked)} /> I have permission to broadcast this</label>}
      </div>

      <div className="spread"><h2>Discovery rules</h2><button className="primary" onClick={() => setDraft(empty())}>New rule</button></div>
      <p className="small muted" style={{ marginTop: -4 }}>Rules search a site on a schedule. What they find goes to the inbox, or straight on air if
        you turn that on for the rule.</p>
      <Loading error={rules.error} loading={rules.loading && !rules.data} />
      <div className="panel scroll">
        <table>
          <thead><tr><th>Rule</th><th>Provider</th><th>Query</th><th>Every</th><th>Last run</th><th></th></tr></thead>
          <tbody>
            {rules.data?.map((r) => (
              <tr key={r.id}>
                <td><b>{r.name}</b> {!r.enabled && <Chip>paused</Chip>} {r.params.auto_air ? <Chip cls="ok">auto on air</Chip> : null}
                  <div className="small muted">{r.kind} · max {r.max_results}</div></td>
                <td>{meta.providers[r.provider] ?? r.provider}</td>
                <td className="small">{r.query || <span className="muted">—</span>}</td>
                <td className="small">{r.scan_interval_minutes >= 60 ? `${r.scan_interval_minutes / 60} h` : `${r.scan_interval_minutes} min`}</td>
                <td className="small">{t(r.last_run_at)} {r.last_status === "error" ? <Chip cls="bad">error</Chip> : r.last_status && <Chip cls="ok">{r.last_status}</Chip>}
                  {" "}{r.last_new_count} new{r.last_error && <div className="muted">{r.last_error.slice(0, 160)}</div>}</td>
                <td className="row">
                  <Act run={async () => {
                    const res = await api.post<{ found: number; new: number; skipped_known: number; filtered: number; errors: string[] }>("/discovery/scan", { rule_id: r.id });
                    rules.reload();
                    if (res.errors.length) throw new Error(`Scan finished with errors: ${res.errors.join("; ")}`);
                    return res;
                  }} ok={(x) => { const res = x as { found: number; new: number; skipped_known: number; filtered: number };
                    return `Scan complete: ${res.found} found, ${res.new} new, ${res.skipped_known} known, ${res.filtered} filtered out.`; }}>Run now</Act>
                  <button onClick={() => edit(r)}>Edit</button>
                  <Act className="danger" confirm={`Delete rule '${r.name}'? Items it found stay in the catalogue.`}
                    run={async () => { await api.del(`/discovery/rules/${r.id}`); rules.reload(); }} ok="Rule deleted.">Delete</Act>
                </td>
              </tr>
            ))}
            {rules.data && !rules.data.length && <tr><td colSpan={6} className="muted">No rules yet.</td></tr>}
          </tbody>
        </table>
      </div>

      {draft && (
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>{draft.id ? `Edit rule #${draft.id}` : "New rule"}</h3>
          <div className="fields">
            <Field label="Name"><input type="text" value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} /></Field>
            <Field label="Site"><select value={draft.provider} disabled={!!draft.id} onChange={(e) => {
              const provider = e.target.value;
              const ok = KINDS.some(([k, , ps]) => k === draft.kind && ps.includes(provider));
              setDraft({ ...draft, provider, kind: ok ? draft.kind : "search" });
            }}>
              {Object.entries(meta.providers).filter(([k]) => k !== "local").map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
            <Field label="Kind"><select value={draft.kind} disabled={!!draft.id} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
              {KINDS.filter(([, , ps]) => ps.includes(draft.provider)).map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
            <Field label="Query / keywords / URL"><input type="text" value={draft.query} onChange={(e) => setDraft({ ...draft, query: e.target.value })} /></Field>
            <Field label="Max results per scan"><input type="number" min={1} max={500} value={draft.max_results}
              onChange={(e) => setDraft({ ...draft, max_results: Number(e.target.value) })} /></Field>
            <Field label="Scan every (minutes)"><input type="number" min={15} value={draft.scan_interval_minutes}
              onChange={(e) => setDraft({ ...draft, scan_interval_minutes: Number(e.target.value) })} /></Field>
          </div>
          <details className="advanced" style={{ margin: "12px 0" }}>
          <summary><b>Filters and preferences</b> <span className="small muted">optional</span></summary>
          <div className="fields" style={{ marginTop: 8 }}>
            {TEXT_PARAMS.filter(([, , , ps]) => ps.includes(draft.provider)).map(([k, l, ph]) => (
              <Field key={k} label={l}><input type="text" placeholder={ph} value={String(draft.params[k] ?? "")} onChange={(e) => setP(k, e.target.value)} /></Field>
            ))}
            <Field label="Min duration (minutes)"><input type="number" min={0} step="0.5" value={String(draft.params.min_duration ?? "")} onChange={(e) => setP("min_duration", e.target.value)} /></Field>
            <Field label="Max duration (minutes)"><input type="number" min={0} step="0.5" value={String(draft.params.max_duration ?? "")} onChange={(e) => setP("max_duration", e.target.value)} /></Field>
            <Field label="Preferred resolution"><select value={String(draft.params.preferred_height ?? "")} onChange={(e) => setP("preferred_height", e.target.value)}>
              <option value="">Any (format order only)</option>{[360, 480, 576, 720, 1080].map((h) => <option key={h} value={h}>{h}p</option>)}</select></Field>
          </div>
          {FLAGS.filter(([, , ps]) => ps.includes(draft.provider)).map(([k, l]) => (
            <label key={k} className="inline"><input type="checkbox" checked={!!draft.params[k]} onChange={(e) => setP(k, e.target.checked)} /> {l}</label>
          ))}
          </details>
          <label className="inline"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <h3>Channels</h3>
          <p className="small muted">What this rule finds is added to these channels when it's put on air.</p>
          {channels.data?.map((c) => (
            <label key={c.id} className="inline"><input type="checkbox" checked={draft.channel_ids.includes(c.id)} onChange={(e) =>
              setDraft({ ...draft, channel_ids: e.target.checked ? [...draft.channel_ids, c.id] : draft.channel_ids.filter((x) => x !== c.id) })} /> {c.name}</label>
          ))}
          <div className="panel auto-air">
            <label className="inline"><input type="checkbox" checked={!!draft.params.auto_air} onChange={(e) => setP("auto_air", e.target.checked)} />
              <b>Put new finds on air automatically</b> (skip the inbox)</label>
            <p className="small muted" style={{ margin: "4px 0 0" }}>Every new video that plays correctly is approved and added to the channels above.
              Only use this for sources you have permission to broadcast. Videos that fail the playback check still go to the inbox.</p>
            {!!draft.params.auto_air && !draft.channel_ids.length && <p className="small warn-text">Pick at least one channel above, or finds
              will be approved but not shown anywhere.</p>}
          </div>
          <p className="row"><Act className="primary" disabled={!draft.name.trim()} run={save} ok="Rule saved.">Save rule</Act>
            <button onClick={() => setDraft(null)}>Cancel</button></p>
        </div>
      )}
    </>
  );
}
