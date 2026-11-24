import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, qs, type ChannelDetail, type MediaSummary, type Paged, type Rule } from "../api";
import { Act, Chip, Field, fmtDur, Loading, StatusChip, useLoad, useMeta } from "../ui";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function ChannelPage() {
  const id = Number(useParams().id);
  const ch = useLoad(() => api.get<ChannelDetail>(`/channels/${id}`), [id]);
  if (!ch.data) return <Loading error={ch.error} loading={ch.loading} />;
  const c = ch.data;
  return (
    <>
      <div className="spread">
        <h1>{c.name} <span className="small muted"><code>{c.slug}</code></span></h1>
        <span className="row">
          <Link className="btn" to={`/schedule?channel=${c.id}`}>Schedule</Link>
          <Act run={async () => api.post<{ added: number }>(`/channels/${c.id}/schedule/generate?mode=regenerate`)}
            confirm="Rebuild all upcoming entries? The programme on air now is kept."
            ok={(r) => `Schedule regenerated (${(r as { added: number }).added} entries).`}>Regenerate schedule</Act>
        </span>
      </div>
      <div className="two">
        <div>
          <SettingsForm c={c} onSaved={ch.reload} />
          <ItemsPanel c={c} setC={ch.setData} />
        </div>
        <div>
          <LookPanel c={c} />
          <LogoPanel c={c} setC={ch.setData} />
          <BlocksPanel c={c} setC={ch.setData} reload={ch.reload} />
          <DeletePanel c={c} />
        </div>
      </div>
    </>
  );
}

function SettingsForm({ c, onSaved }: { c: ChannelDetail; onSaved: () => void }) {
  const meta = useMeta();
  const rules = useLoad(() => api.get<Rule[]>("/discovery/rules"), []);
  const init = () => ({ ...c, number: c.number?.toString() ?? "", logo_url: c.logo_url ?? "", description: c.description ?? "",
    fallback_item_id: c.fallback_item_id?.toString() ?? "", card_theme: c.card_theme ?? "auto" });
  const [f, setF] = useState(init);
  useEffect(() => setF(init()), [c.id]);
  const txt = (k: "name" | "tvg_id" | "group_title" | "logo_url" | "seed" | "description" | "number") =>
    ({ value: f[k] as string, onChange: (e: { target: { value: string } }) => setF({ ...f, [k]: e.target.value }) });
  const chk = (k: "enabled" | "idents_between" | "filler_pad" | "catchup_enabled" | "breaks_enabled") =>
    ({ checked: f[k], onChange: (e: { target: { checked: boolean } }) => setF({ ...f, [k]: e.target.checked }) });
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Settings</h3>
      <div className="fields">
        <Field label="Name"><input type="text" {...txt("name")} /></Field>
        <Field label="Number"><input type="number" {...txt("number")} /></Field>
        <Field label="TVG ID"><input type="text" {...txt("tvg_id")} /></Field>
        <Field label="Group"><input type="text" {...txt("group_title")} /></Field>
        <Field label="Logo URL (an uploaded logo takes precedence)"><input type="url" {...txt("logo_url")} /></Field>
        <Field label="Mode"><select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>{meta.modes.map((m) => <option key={m}>{m}</option>)}</select></Field>
        <Field label="Shuffle seed"><input type="text" {...txt("seed")} /></Field>
        <Field label="Avoid repeating last N items"><input type="number" min={0} value={f.repeat_avoid_count}
          onChange={(e) => setF({ ...f, repeat_avoid_count: Number(e.target.value) })} /></Field>
        <Field label="Output"><select value={f.playback_format} onChange={(e) => setF({ ...f, playback_format: e.target.value })}>
          {meta.formats.map((m) => <option key={m}>{m}</option>)}</select></Field>
        <Field label="Fallback programme"><select value={f.fallback_item_id} onChange={(e) => setF({ ...f, fallback_item_id: e.target.value })}>
          <option value="">— generated “off air” card —</option>
          {c.items.map((i) => <option key={i.id} value={i.media_item_id}>{i.title?.slice(0, 60)}</option>)}</select></Field>
        <Field label="Look (break cards and drawn logo)"><select value={f.card_theme} onChange={(e) => setF({ ...f, card_theme: e.target.value })}>
          {meta.looks.map((l) => <option key={l.key} value={l.key}>{l.label}</option>)}</select></Field>
        <Field label="Break style"><select value={f.break_style} onChange={(e) => setF({ ...f, break_style: e.target.value as ChannelDetail["break_style"] })}>
          <option value="auto">Automatic (from the look)</option>
          <option value="bumper">Animated “Coming up next” bumper</option>
          <option value="lineup">“Up next” lineup card</option></select></Field>
        <Field label="Break card length (seconds)"><input type="number" min={4} max={120} value={f.break_seconds}
          onChange={(e) => setF({ ...f, break_seconds: Number(e.target.value) })} /></Field>
        <Field label="Programme history (catch-up window)"><select value={f.catchup_days} onChange={(e) => setF({ ...f, catchup_days: Number(e.target.value) })}>
          {meta.retention_choices.map((d) => <option key={d} value={d}>{d} day{d > 1 ? "s" : ""}</option>)}</select></Field>
      </div>
      <Field label="Description"><textarea {...txt("description")} /></Field>
      <label className="inline"><input type="checkbox" {...chk("enabled")} /> Enabled (published in the feeds)</label>
      <label className="inline"><input type="checkbox" {...chk("breaks_enabled")} /> “Up next” break card before each programme</label>
      <label className="inline"><input type="checkbox" {...chk("idents_between")} /> Station ident between programmes</label>
      <label className="inline"><input type="checkbox" {...chk("filler_pad")} /> Pad to block starts with filler</label>
      <label className="inline"><input type="checkbox" {...chk("catchup_enabled")} /> Catch-up and Start Over (nothing is recorded)</label>
      <h3>Discovery rules feeding this channel</h3>
      {rules.data?.map((r) => (
        <label key={r.id} className="inline small"><input type="checkbox" checked={f.rule_ids.includes(r.id)} onChange={(e) =>
          setF({ ...f, rule_ids: e.target.checked ? [...f.rule_ids, r.id] : f.rule_ids.filter((x) => x !== r.id) })} /> {r.name}</label>
      ))}
      {rules.data && !rules.data.length && <span className="small muted">No rules.</span>}
      <p><Act className="primary" run={async () => {
        await api.patch(`/channels/${c.id}`, {
          name: f.name, tvg_id: f.tvg_id, group_title: f.group_title, logo_url: f.logo_url || null, mode: f.mode, seed: f.seed,
          repeat_avoid_count: f.repeat_avoid_count, playback_format: f.playback_format, number: f.number ? Number(f.number) : null,
          fallback_item_id: f.fallback_item_id ? Number(f.fallback_item_id) : null, catchup_days: f.catchup_days, description: f.description || null,
          enabled: f.enabled, idents_between: f.idents_between, filler_pad: f.filler_pad, catchup_enabled: f.catchup_enabled, rule_ids: f.rule_ids,
          breaks_enabled: f.breaks_enabled, break_seconds: f.break_seconds, card_theme: f.card_theme, break_style: f.break_style,
        });
        onSaved();
      }} ok="Channel saved. Upcoming entries update as content changes; Regenerate to rebuild now.">Save channel</Act></p>
    </div>
  );
}

function ItemsPanel({ c, setC }: { c: ChannelDetail; setC: (d: ChannelDetail) => void }) {
  const meta = useMeta();
  const [q, setQ] = useState("");
  const [role, setRole] = useState("programme");
  const [pick, setPick] = useState<number[]>([]);
  const approved = useLoad(() => api.get<Paged<MediaSummary>>(`/catalogue${qs({ status: "APPROVED", q, per_page: 100, sort: "title" })}`), [q]);
  const have = new Set(c.items.map((i) => i.media_item_id));
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Content ({c.items.length})</h3>
      <div className="scroll">
        <table>
          <thead><tr><th>Pos</th><th>Item</th><th>Role</th><th>Duration</th><th>Airs?</th><th></th></tr></thead>
          <tbody>
            {c.items.map((i) => (
              <tr key={i.id}>
                <td><input type="number" defaultValue={i.position} style={{ width: 64 }} onBlur={async (e) => {
                  if (Number(e.target.value) !== i.position) setC(await api.patch<ChannelDetail>(`/channels/${c.id}/items/${i.id}`, { position: Number(e.target.value) }));
                }} /></td>
                <td><Link to={`/items/${i.media_item_id}`}>{i.title}</Link>{i.review_status && i.review_status !== "APPROVED" && <> <StatusChip s={i.review_status} /></>}</td>
                <td><select value={i.role} onChange={async (e) => setC(await api.patch<ChannelDetail>(`/channels/${c.id}/items/${i.id}`, { role: e.target.value }))}>
                  {meta.roles.map((r) => <option key={r}>{r}</option>)}</select></td>
                <td className="small">{fmtDur(i.duration)}</td>
                <td>{i.schedulable ? <Chip cls="ok">eligible</Chip> : <Chip cls="warn" >not eligible</Chip>}</td>
                <td><Act className="danger" run={async () => setC(await api.del<ChannelDetail>(`/channels/${c.id}/items/${i.id}`))}>Remove</Act></td>
              </tr>
            ))}
            {!c.items.length && <tr><td colSpan={6} className="muted">No content yet. Add approved items below.</td></tr>}
          </tbody>
        </table>
      </div>
      <h3>Add approved items</h3>
      <div className="toolbar">
        <input type="text" placeholder="Filter approved items…" onKeyDown={(e) => e.key === "Enter" && setQ((e.target as HTMLInputElement).value)} />
        <select value={role} onChange={(e) => setRole(e.target.value)}>{meta.roles.map((r) => <option key={r}>{r}</option>)}</select>
        <Act className="primary" disabled={!pick.length} run={async () => {
          setC(await api.post<ChannelDetail>(`/channels/${c.id}/items`, { item_ids: pick, role }));
          setPick([]);
        }} ok="Added.">Add {pick.length} selected</Act>
      </div>
      <div style={{ maxHeight: 260, overflow: "auto" }}>
        {approved.data?.items.filter((m) => !have.has(m.id)).map((m) => (
          <label key={m.id} className="small" style={{ display: "flex", gap: 6 }}>
            <input type="checkbox" checked={pick.includes(m.id)} onChange={(e) => setPick(e.target.checked ? [...pick, m.id] : pick.filter((x) => x !== m.id))} />
            {m.title} <span className="muted">{fmtDur(m.duration)}</span> {m.schedulable ? null : <Chip cls="warn">{m.iptv_status}</Chip>}
          </label>
        ))}
      </div>
    </div>
  );
}

function LookPanel({ c }: { c: ChannelDetail }) {
  const meta = useMeta();
  const [v, setV] = useState(0);
  useEffect(() => setV((x) => x + 1), [c.look, c.name, c.number]);
  const look = meta.looks.find((l) => l.key === c.look)?.label ?? c.look;
  return (
    <div className="panel">
      <div className="spread"><h3 style={{ margin: 0 }}>{c.break_style_resolved === "bumper" ? "Coming up next bumper" : "Up next card"}</h3>
        <button type="button" onClick={() => setV(v + 1)}>Refresh</button></div>
      <img key={v} src={`/api/v1/channels/${c.id}/card-preview.png?v=${v}`} alt={`Up next card for ${c.name}`}
        style={{ width: "100%", aspectRatio: "16/9", borderRadius: 6, background: "var(--chip)", marginTop: 8 }} />
      <p className="small muted">Look: <b>{look}</b>{c.card_theme === "auto" ? " (automatic)" : ""}. The next three programmes as the card shows them now
        {c.break_style_resolved === "bumper" ? " (a still: on air the bumper animates and plays a few seconds of the next show)" : ""}.
        {c.breaks_enabled ? ` Airs for ${c.break_seconds}s before each programme.` : " Break cards are off for this channel (see Settings)."}</p>
    </div>
  );
}

function LogoPanel({ c, setC }: { c: ChannelDetail; setC: (d: ChannelDetail) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const source = c.logo_file ? (c.logo_generated ? "Drawn in the channel’s look" : "Uploaded") : c.logo ? "From the logo URL" : null;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Logo</h3>
      {c.logo ? <img src={c.logo_file ? `/logos/${c.logo_file}` : c.logo} alt={`${c.name} logo`} style={{ width: 128, height: 128, objectFit: "contain", borderRadius: 6 }} />
        : <p className="small muted">No logo yet: one is drawn on the next schedule run.</p>}
      {source && <p className="small muted">{source}. An upload wins, then a logo URL, otherwise ArchiveTV draws one.</p>}
      <div className="row" style={{ marginTop: 8 }}>
        <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <Act disabled={!file} run={async () => {
          const fd = new FormData();
          fd.append("file", file!);
          setC(await api.post<ChannelDetail>(`/channels/${c.id}/logo`, fd));
        }} ok="Logo uploaded. Refresh the account in Dispatcharr to pick it up.">Upload</Act>
        <Act run={async () => setC(await api.post<ChannelDetail>(`/channels/${c.id}/logo/generate`))}
          confirm={c.logo_file && !c.logo_generated ? "Replace the uploaded logo with a drawn one?" : undefined}
          ok="Logo drawn in the channel’s look.">{c.logo_generated ? "Redraw" : "Draw a logo"}</Act>
        {c.logo_file && !c.logo_generated && <Act className="danger" run={async () => setC(await api.del<ChannelDetail>(`/channels/${c.id}/logo`))}
          ok="Uploaded logo removed.">Remove upload</Act>}
      </div>
      <p className="small muted">PNG, JPEG or WebP up to 1 MB. Used in the M3U, XMLTV, Xtream Codes feeds and the public guide.</p>
    </div>
  );
}

function BlocksPanel({ c, setC, reload }: { c: ChannelDetail; setC: (d: ChannelDetail) => void; reload: () => void }) {
  const [b, setB] = useState({ name: "", start_time: "08:00", end_time: "10:00", days: "0123456", tag_filter: "", mode: "shuffle" });
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Time blocks</h3>
      <p className="small muted">Recurring wall-clock blocks in the display timezone (used in block mode). Outside blocks the channel shuffles.</p>
      {c.blocks.map((x) => (
        <div key={x.id} className="spread small">
          <span><b>{x.name}</b> {x.start_time}–{x.end_time} · {x.days.split("").map((d) => DAYS[Number(d)]).join(" ")} · {x.mode}
            {x.tag_filter && <> · tags <code>{x.tag_filter}</code></>}</span>
          <Act className="danger" run={async () => { await api.del(`/blocks/${x.id}`); reload(); }}>×</Act>
        </div>
      ))}
      <div className="fields" style={{ marginTop: 8 }}>
        <Field label="Name"><input type="text" value={b.name} onChange={(e) => setB({ ...b, name: e.target.value })} placeholder="Morning cartoons" /></Field>
        <Field label="Start"><input type="time" value={b.start_time} onChange={(e) => setB({ ...b, start_time: e.target.value })} /></Field>
        <Field label="End"><input type="time" value={b.end_time} onChange={(e) => setB({ ...b, end_time: e.target.value })} /></Field>
        <Field label="Tag filter (comma)"><input type="text" value={b.tag_filter} onChange={(e) => setB({ ...b, tag_filter: e.target.value })} /></Field>
        <Field label="Order"><select value={b.mode} onChange={(e) => setB({ ...b, mode: e.target.value })}><option>shuffle</option><option>sequential</option></select></Field>
      </div>
      <div className="row small">
        {DAYS.map((d, i) => (
          <label key={d} className="inline"><input type="checkbox" checked={b.days.includes(String(i))} onChange={(e) =>
            setB({ ...b, days: e.target.checked ? [...b.days, String(i)].sort().join("") : b.days.replace(String(i), "") })} />{d}</label>
        ))}
      </div>
      <p><Act disabled={!b.name || !b.days} run={async () => {
        setC(await api.post<ChannelDetail>(`/channels/${c.id}/blocks`, { ...b, tag_filter: b.tag_filter || null }));
        setB({ ...b, name: "" });
      }} ok="Block added. Regenerate to apply it to the published schedule.">Add block</Act></p>
    </div>
  );
}

function DeletePanel({ c }: { c: ChannelDetail }) {
  const nav = useNavigate();
  const [confirm, setConfirm] = useState("");
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Delete channel</h3>
      <p className="small muted">Removes the channel and its schedule. Catalogue items are untouched. Type <code>{c.slug}</code> to confirm.</p>
      <div className="row"><input type="text" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
        <Act className="danger" disabled={confirm !== c.slug} run={async () => { await api.del(`/channels/${c.id}?confirm=${encodeURIComponent(confirm)}`); nav("/channels"); }}
          ok="Channel deleted.">Delete</Act></div>
    </div>
  );
}
