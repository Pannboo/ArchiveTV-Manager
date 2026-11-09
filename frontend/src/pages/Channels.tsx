import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Channel } from "../api";
import { Act, Chip, Field, Loading, MODE_LABEL, useLoad, useMeta } from "../ui";

/** "Classic Cinema!" → "classic-cinema" (the channel's stable id in URLs and feeds). */
const slugify = (s: string) => s.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 60);

export default function Channels() {
  const meta = useMeta();
  const nav = useNavigate();
  const list = useLoad(() => api.get<Channel[]>("/channels"), []);
  const [f, setF] = useState({ name: "", slug: "", number: "", group_title: "ArchiveTV", mode: "sequential" });
  const [slugEdited, setSlugEdited] = useState(false);
  const slug = slugEdited ? f.slug : slugify(f.name);
  const channels = list.data ?? [];
  return (
    <>
      <h1>Channels</h1>
      <Loading error={list.error} loading={list.loading && !list.data} />
      {list.data && !channels.length && (
        <div className="panel getting-started">
          <h3 style={{ marginTop: 0 }}>Get a channel on air in two steps</h3>
          <ol>
            <li>Create a channel below. A name is all it needs.</li>
            <li>On the channel's page, paste links to Internet Archive or YouTube videos. They're checked and go straight on air.</li>
          </ol>
          <p className="small muted">Then add ArchiveTV to Dispatcharr as an Xtream Codes account (see the README).</p>
        </div>
      )}
      <div className="channel-cards">
        {channels.map((c) => (
          <Link key={c.id} to={`/channels/${c.id}`} className="panel channel-card">
            <div className="spread"><b>{c.number != null && <span className="muted">{c.number} · </span>}{c.name}</b>
              {!c.enabled && <Chip cls="warn">disabled</Chip>}</div>
            <div className="small">{c.now_playing ? <>On now: {c.now_playing}</> : <span className="muted">Nothing on air</span>}</div>
            <div className="small muted">{c.items} item{c.items === 1 ? "" : "s"} · {MODE_LABEL[c.mode] ?? c.mode}
              {c.catchup_enabled ? ` · catch-up ${c.catchup_days} days` : ""}</div>
          </Link>
        ))}
      </div>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>New channel</h3>
        <div className="fields">
          <Field label="Name"><input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Classic Cinema" autoFocus={!channels.length} /></Field>
          <Field label="Channel number (optional)"><input type="number" value={f.number} onChange={(e) => setF({ ...f, number: e.target.value })} /></Field>
          <Field label="Order"><select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>
            {meta.modes.map((m) => <option key={m} value={m}>{MODE_LABEL[m] ?? m}</option>)}</select></Field>
        </div>
        <details className="small" style={{ marginTop: 6 }}>
          <summary className="muted">More options</summary>
          <div className="fields" style={{ marginTop: 6 }}>
            <Field label="Channel ID (stays the same forever; used in stream links)"><input type="text" value={slug}
              onChange={(e) => { setSlugEdited(true); setF({ ...f, slug: e.target.value.toLowerCase() }); }} /></Field>
            <Field label="Group in Dispatcharr"><input type="text" value={f.group_title} onChange={(e) => setF({ ...f, group_title: e.target.value })} /></Field>
          </div>
        </details>
        <p><Act className="primary" disabled={!f.name.trim() || !slug} run={async () => {
          const c = await api.post<Channel>("/channels", { name: f.name.trim(), slug, group_title: f.group_title, mode: f.mode,
            number: f.number ? Number(f.number) : null });
          nav(`/channels/${c.id}`);
        }} ok="Channel created. Now paste some links to put videos on it.">Create channel</Act></p>
      </div>
    </>
  );
}
