import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type Channel, type Rule } from "../api";
import { Act, Chip, Field, Loading, useLoad, useMeta, useTime, useToast } from "../ui";

const KINDS: [string, string][] = [
  ["search", "Keyword search"], ["collection", "Archive collection"], ["channel", "YouTube channel uploads"],
  ["playlist", "YouTube playlist"], ["video", "Single item / URL"],
];
const TEXT_PARAMS: [string, string, string?][] = [
  ["collection", "Archive collection(s), ; separated", "classic_tv"], ["creator", "Archive creator"], ["subject", "Archive subject"],
  ["year_from", "Year from"], ["year_to", "Year to"], ["channel", "YouTube channel (@handle, URL or UC id)"],
  ["playlist", "YouTube playlist (URL or id)"], ["published_after", "Published after (YYYY-MM-DD)"],
  ["published_before", "Published before (YYYY-MM-DD)"], ["categories", "YouTube categories (ids or names)", "1, 24"],
  ["excluded_channels", "Excluded channels/creators (comma)"], ["excluded_keywords", "Excluded keywords (comma)"],
  ["sort", "Archive sort", "addeddate desc"], ["preferred_formats", "Preferred formats, best first", "h.264, mpeg4"],
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
      <h1>Discovery sources</h1>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Import by URL</h3>
        <div className="row">
          <input type="text" value={url} onChange={(e) => setUrl(e.target.value)} style={{ flex: 1, minWidth: 260 }}
            placeholder="https://archive.org/details/…, a YouTube video, playlist, @channel, or @channel/live URL" />
          <Act className="primary" disabled={!url.trim()} run={async () => {
            const r = await api.post<{ new: number; skipped_known: number; errors: string[] }>("/discovery/import", { url });
            if (r.errors.length) throw new Error(`Import failed: ${r.errors.join("; ")}`);
            notify(r.new ? `Imported ${r.new} item(s) into the review queue.` : `Nothing new (${r.skipped_known} already in catalogue).`);
            setUrl("");
            if (r.new) nav("/review");
          }}>Import</Act>
        </div>
      </div>

      <div className="spread"><h2>Rules</h2><button className="primary" onClick={() => setDraft(empty())}>New rule</button></div>
      <Loading error={rules.error} loading={rules.loading && !rules.data} />
      <div className="panel scroll">
        <table>
          <thead><tr><th>Rule</th><th>Provider</th><th>Query</th><th>Every</th><th>Last run</th><th></th></tr></thead>
          <tbody>
            {rules.data?.map((r) => (
              <tr key={r.id}>
                <td><b>{r.name}</b> {!r.enabled && <Chip>paused</Chip>}<div className="small muted">{r.kind} · max {r.max_results}</div></td>
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
            <Field label="Provider"><select value={draft.provider} disabled={!!draft.id} onChange={(e) => setDraft({ ...draft, provider: e.target.value })}>
              {Object.entries(meta.providers).filter(([k]) => k !== "local").map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
            <Field label="Kind"><select value={draft.kind} disabled={!!draft.id} onChange={(e) => setDraft({ ...draft, kind: e.target.value })}>
              {KINDS.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</select></Field>
            <Field label="Query / keywords / URL"><input type="text" value={draft.query} onChange={(e) => setDraft({ ...draft, query: e.target.value })} /></Field>
            <Field label="Max results per scan"><input type="number" min={1} max={500} value={draft.max_results}
              onChange={(e) => setDraft({ ...draft, max_results: Number(e.target.value) })} /></Field>
            <Field label="Scan every (minutes)"><input type="number" min={15} value={draft.scan_interval_minutes}
              onChange={(e) => setDraft({ ...draft, scan_interval_minutes: Number(e.target.value) })} /></Field>
          </div>
          <h3>Filters and preferences</h3>
          <div className="fields">
            {TEXT_PARAMS.map(([k, l, ph]) => (
              <Field key={k} label={l}><input type="text" placeholder={ph} value={String(draft.params[k] ?? "")} onChange={(e) => setP(k, e.target.value)} /></Field>
            ))}
            <Field label="Min duration (minutes)"><input type="number" min={0} step="0.5" value={String(draft.params.min_duration ?? "")} onChange={(e) => setP("min_duration", e.target.value)} /></Field>
            <Field label="Max duration (minutes)"><input type="number" min={0} step="0.5" value={String(draft.params.max_duration ?? "")} onChange={(e) => setP("max_duration", e.target.value)} /></Field>
            <Field label="Preferred resolution"><select value={String(draft.params.preferred_height ?? "")} onChange={(e) => setP("preferred_height", e.target.value)}>
              <option value="">Any (format order only)</option>{[360, 480, 576, 720, 1080].map((h) => <option key={h} value={h}>{h}p</option>)}</select></Field>
          </div>
          {([["licensed_only", "Archive: only items with a declared licence"], ["embeddable_only", "YouTube: embeddable only"],
            ["skip_validation", "Skip automatic validation (FFprobe) — unvalidated items cannot be scheduled"]] as const).map(([k, l]) => (
            <label key={k} className="inline"><input type="checkbox" checked={!!draft.params[k]} onChange={(e) => setP(k, e.target.checked)} /> {l}</label>
          ))}
          <label className="inline"><input type="checkbox" checked={draft.enabled} onChange={(e) => setDraft({ ...draft, enabled: e.target.checked })} /> Enabled</label>
          <p className="small muted">Manual approval is always required: discovered items are never scheduled until an administrator approves them.</p>
          <h3>Linked channels</h3>
          <p className="small muted">Items from this rule are added to these channels when you approve them.</p>
          {channels.data?.map((c) => (
            <label key={c.id} className="inline"><input type="checkbox" checked={draft.channel_ids.includes(c.id)} onChange={(e) =>
              setDraft({ ...draft, channel_ids: e.target.checked ? [...draft.channel_ids, c.id] : draft.channel_ids.filter((x) => x !== c.id) })} /> {c.name}</label>
          ))}
          <p className="row"><Act className="primary" disabled={!draft.name.trim()} run={save} ok="Rule saved.">Save rule</Act>
            <button onClick={() => setDraft(null)}>Cancel</button></p>
        </div>
      )}
    </>
  );
}
