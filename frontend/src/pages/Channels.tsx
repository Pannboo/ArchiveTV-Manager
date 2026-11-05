import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, type Channel } from "../api";
import { Act, Chip, Field, Loading, useLoad, useMeta } from "../ui";

export default function Channels() {
  const meta = useMeta();
  const nav = useNavigate();
  const list = useLoad(() => api.get<Channel[]>("/channels"), []);
  const [f, setF] = useState({ slug: "", name: "", number: "", group_title: "ArchiveTV", mode: "sequential" });
  return (
    <>
      <h1>Channels</h1>
      <Loading error={list.error} loading={list.loading && !list.data} />
      <div className="panel scroll">
        <table>
          <thead><tr><th>#</th><th>Channel</th><th>Mode</th><th>Items</th><th>Catch-up</th><th>On now</th></tr></thead>
          <tbody>
            {list.data?.map((c) => (
              <tr key={c.id}>
                <td>{c.number ?? ""}</td>
                <td><Link to={`/channels/${c.id}`}>{c.name}</Link> {!c.enabled && <Chip>disabled</Chip>}
                  <div className="small muted"><code>{c.slug}</code> · tvg-id <code>{c.tvg_id}</code> · {c.group_title}</div></td>
                <td className="small">{c.mode} · {c.playback_format}</td>
                <td>{c.items}</td>
                <td>{c.catchup_enabled ? <Chip cls="ok">{c.catchup_days} days</Chip> : <Chip>off</Chip>}</td>
                <td className="small">{c.now_playing ?? <span className="muted">—</span>}</td>
              </tr>
            ))}
            {list.data && !list.data.length && <tr><td colSpan={6} className="muted">No channels yet.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>New channel</h3>
        <div className="fields">
          <Field label="Channel ID (lowercase, digits, dashes)"><input type="text" value={f.slug} onChange={(e) => setF({ ...f, slug: e.target.value.toLowerCase() })} placeholder="classic-cinema" /></Field>
          <Field label="Name"><input type="text" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} placeholder="Classic Cinema" /></Field>
          <Field label="Number"><input type="number" value={f.number} onChange={(e) => setF({ ...f, number: e.target.value })} /></Field>
          <Field label="Group"><input type="text" value={f.group_title} onChange={(e) => setF({ ...f, group_title: e.target.value })} /></Field>
          <Field label="Mode"><select value={f.mode} onChange={(e) => setF({ ...f, mode: e.target.value })}>{meta.modes.map((m) => <option key={m}>{m}</option>)}</select></Field>
        </div>
        <p><Act className="primary" disabled={!f.slug || !f.name} run={async () => {
          const c = await api.post<Channel>("/channels", { ...f, number: f.number ? Number(f.number) : null });
          nav(`/channels/${c.id}`);
        }} ok="Channel created. Assign approved content and it will appear in the feeds.">Create channel</Act></p>
      </div>
    </>
  );
}
