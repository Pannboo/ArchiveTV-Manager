import { useEffect, useState } from "react";
import { api } from "../api";
import { Act, Chip, Field, Loading, useLoad, useTime } from "../ui";

interface SettingsData {
  base_url: string;
  tunables: { key: string; type: "str" | "int" | "bool"; label: string; help: string; value: string | number | boolean | null }[];
  xtream: { server_url: string; username: string; password_source: string };
  feeds: { m3u: string; xmltv: string; token_set: boolean };
  youtube_configured: boolean;
  provider_status: Record<string, { quota_used: number; quota_exhausted_until: string | null }>;
  media_dir: string;
  allowed_hosts: string[];
  minimal_disk: boolean;
  backups: string[];
}

export default function Settings() {
  const t = useTime();
  const s = useLoad(() => api.get<SettingsData>("/settings"), []);
  const [vals, setVals] = useState<Record<string, string | number | boolean | null>>({});
  useEffect(() => {
    if (s.data) setVals(Object.fromEntries(s.data.tunables.map((x) => [x.key, x.value])));
  }, [s.data]);
  if (!s.data) return <Loading error={s.error} loading={s.loading} />;
  const d = s.data;
  const yt = d.provider_status.youtube;
  return (
    <>
      <h1>Settings</h1>
      <div className="grid">
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Dispatcharr: Xtream Codes account (live + catch-up)</h3>
          <p className="small">Dispatcharr only passes catch-up through for <b>Xtream Codes</b> accounts. Add ArchiveTV in
            <i> M3U &amp; EPG Manager → Add M3U → type Xtream Codes</i>:</p>
          <dl className="kv">
            <dt>Server URL</dt><dd><code>{d.xtream.server_url}</code></dd>
            <dt>Username</dt><dd><code>{d.xtream.username}</code></dd>
            <dt>Password</dt><dd>{d.xtream.password_source === "ATV_XC_PASSWORD" ? <>from <code>ATV_XC_PASSWORD</code></> : <code>{d.xtream.password_source}</code>}</dd>
            <dt>EPG (XMLTV)</dt><dd><code>{d.xtream.server_url}/xmltv.php?username={d.xtream.username}&amp;password=…</code></dd>
          </dl>
          <p className="small muted">Then raise Dispatcharr's <i>Settings → EPG → Default days back</i> (0 by default), or past programmes never reach your player.</p>
          <h3>Plain M3U feeds (live only)</h3>
          <dl className="kv"><dt>M3U</dt><dd><code>{d.feeds.m3u}</code></dd><dt>XMLTV</dt><dd><code>{d.feeds.xmltv}</code></dd></dl>
          <p className="small muted">Feed token: {d.feeds.token_set ? "set (ATV_FEED_TOKEN)" : "not set — feeds are open to anything that can reach this port"}.</p>
        </div>
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Credentials and storage</h3>
          <p className="small">YouTube Data API key: {d.youtube_configured ? <Chip cls="ok">configured</Chip> : <Chip cls="bad">not set</Chip>}
            {yt && <> · quota used today: <b>{yt.quota_used}</b>{yt.quota_exhausted_until && ` · exhausted until ${t(yt.quota_exhausted_until)}`}</>}</p>
          <p className="small muted">API keys and passwords come from environment variables or Docker secrets only. They are never stored in the
            database or sent to the browser.</p>
          <p className="small">Storage: {d.minimal_disk ? <><Chip cls="ok">minimal-disk mode</Chip> nothing is downloaded, cached to disk or recorded</>
            : <Chip cls="warn">minimal-disk mode off</Chip>}</p>
          <p className="small">Media folder: <code>{d.media_dir}</code><br />Allowed remote media hosts: {d.allowed_hosts.map((h) => <code key={h}>{h} </code>)}
            <br /><span className="muted">Add hosts with <code>ATV_ALLOWED_MEDIA_HOSTS</code>. YouTube hosts can never be media sources.</span></p>
        </div>
        <div className="panel">
          <h3 style={{ marginTop: 0 }}>Backups</h3>
          <Act run={async () => { const r = await api.post<{ backup: string }>("/system/backup"); s.reload(); return r; }}
            ok={(r) => `Backup written: ${(r as { backup: string }).backup}`}>Back up database now</Act>
          {d.backups.map((b) => <div key={b} className="small"><code>{b}</code></div>)}
          {!d.backups.length && <p className="small muted">No backups yet (one is made daily).</p>}
          <p className="small muted"><a href="/api/docs">REST API documentation</a> · <a href="/admin">Classic admin UI</a></p>
        </div>
      </div>
      <div className="panel">
        <h3 style={{ marginTop: 0 }}>Tunables</h3>
        <div className="fields">
          {d.tunables.map((x) => x.type === "bool" ? (
            <label key={x.key}><span>{x.label}</span><input type="checkbox" checked={!!vals[x.key]} onChange={(e) => setVals({ ...vals, [x.key]: e.target.checked })} />
              {" "}<span className="small muted">{x.help}</span></label>
          ) : (
            <Field key={x.key} label={x.label} hint={x.help}>
              <input type={x.type === "int" ? "number" : "text"} value={vals[x.key] === null || vals[x.key] === undefined ? "" : String(vals[x.key])}
                onChange={(e) => setVals({ ...vals, [x.key]: x.type === "int" ? (e.target.value === "" ? null : Number(e.target.value)) : e.target.value })} />
            </Field>
          ))}
        </div>
        <p><Act className="primary" run={async () => { await api.patch("/settings", vals); s.reload(); }} ok="Settings saved.">Save settings</Act></p>
      </div>
    </>
  );
}
