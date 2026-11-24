import { useState } from "react";
import { Link } from "react-router-dom";
import { api, ApiError, type ChannelsSummary, type FollowedChannel, type InstalledPack, type PackSummary, type PacksResponse } from "../api";
import { Act, Chip, fmtTime, Loading, useLoad, useMeta, useToast } from "../ui";

const POLICY: Record<string, string> = { auto_air: "airs licensed finds", inbox: "review first" };
const PROVIDER: Record<string, string> = { archive: "Internet Archive", youtube: "YouTube" };

export default function Packs() {
  const packs = useLoad(() => api.get<PacksResponse>("/packs"), []);
  if (!packs.data) return <Loading error={packs.error} loading={packs.loading} />;
  const d = packs.data;
  return (
    <>
      <h1>Channel packs</h1>
      <p className="muted" style={{ maxWidth: 820 }}>
        A pack is a shareable recipe: discovery rules, time blocks, a look and a rights policy. Installing one creates the channel and
        keeps it filled to its target. Finds go on air by themselves only with a public-domain or Creative Commons licence (or from a
        collection the pack declares public domain); everything else waits in the <Link to="/review">review queue</Link>.
      </p>
      {d.lab_mode
        ? <div className="flash ok">Lab mode is on: packs set to auto-air put every playable find on air, Internet Archive and
            YouTube alike, without a licence check (Settings → Lab mode).</div>
        : <p className="small muted">For a private lab: <Link to="/settings">Settings → Lab mode</Link> lets auto-air packs air every find,
            not only public-domain and Creative Commons ones.</p>}
      {!d.youtube_available && (
        <div className="flash">YouTube rules in packs are skipped until YouTube search is set up (an API key, or unofficial
          discovery under <Link to="/settings">Settings</Link>). Internet Archive rules work regardless.</div>
      )}
      {d.installed.length > 0 && <h2>Installed</h2>}
      <div className="grid">
        {d.installed.map((p) => <InstalledCard key={p.id} p={p} lab={d.lab_mode} reload={packs.reload} />)}
      </div>
      <h2>Starter packs</h2>
      <div className="grid">
        {d.starters.map((p) => <StarterCard key={p.id} p={p} themeCheck={d.theme_check_available} reload={packs.reload} />)}
      </div>
      <ImportPanel themeCheck={d.theme_check_available} reload={packs.reload} />
      {!d.theme_check_available && (
        <p className="small muted">Optional theme check and clean-up: choose an AI backend under <Link to="/settings">Settings</Link> (a local
          model on your network, or Claude). It reads each find's title and description before it airs and sends off-theme ones to review.</p>
      )}
    </>
  );
}

function Meter({ value, target }: { value: number; target: number }) {
  const pct = Math.min(100, Math.round((value / Math.max(target, 1)) * 100));
  return (
    <div className="meter" role="meter" aria-valuenow={value} aria-valuemin={0} aria-valuemax={target}
      aria-label={`${value} of ${target} hours`}>
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

function InstalledCard({ p, lab, reload }: { p: InstalledPack; lab: boolean; reload: () => void }) {
  const meta = useMeta();
  const [target, setTarget] = useState(String(p.target_hours));
  const [days, setDays] = useState(String(p.refresh_days));
  const [pct, setPct] = useState(String(p.rotate_percent));
  const lr = p.last_refresh;
  const patch = async (body: Record<string, unknown>) => { await api.patch(`/packs/${p.id}`, body); reload(); };
  const status = p.last_fill_status;
  return (
    <div className="panel">
      <div className="spread">
        <h3 style={{ margin: 0 }}>{p.name}</h3>
        <span className="row">
          <Chip cls={p.policy === "auto_air" ? "ok" : undefined}>{p.policy === "auto_air" && lab ? "airs every find" : POLICY[p.policy]}</Chip>
          {status && <Chip cls={status === "error" ? "bad" : status === "full" ? "ok" : "warn"}>{status}</Chip>}
        </span>
      </div>
      {p.channel && <p className="small" style={{ margin: "4px 0 8px" }}>
        <Link to={`/channels/${p.channel.id}`}>{p.channel.number ? `${p.channel.number} · ` : ""}{p.channel.name}</Link>
        {!p.channel.enabled && <> <Chip cls="warn">channel disabled</Chip></>}</p>}
      <div className="spread small"><span>{p.playable_hours} of {p.target_hours} hours on air</span>
        <span className="muted">{p.aired_count} aired · {p.inbox_count} to review</span></div>
      <Meter value={p.playable_hours} target={p.target_hours} />
      {p.waiting_review > 0 && <p className="small"><Link to="/review">{p.waiting_review} waiting in the review queue</Link></p>}
      {p.last_error && <p className="small" style={{ color: "var(--bad)" }}>{p.last_error}</p>}
      <p className="small muted" style={{ margin: "4px 0" }}>
        {p.last_refresh_at
          ? <>Last refresh {fmtTime(p.last_refresh_at, meta.timezone)}: {lr.checked ?? 0} checked, {lr.gone ?? 0} gone,
            {" "}{lr.new ?? 0} new, {lr.rotated_out ?? 0} rested, {lr.rotated_back ?? 0} back{lr.back_to_review ? `, ${lr.back_to_review} back to review` : ""}.</>
          : "No weekly refresh yet."}
        {" "}Next {fmtTime(p.next_refresh_at, meta.timezone)}.{p.resting ? ` ${p.resting} resting.` : ""}
      </p>
      <div className="fields" style={{ marginTop: 8 }}>
        <label><span>Target hours</span>
          <input type="number" min={1} value={target} onChange={(e) => setTarget(e.target.value)}
            onBlur={() => Number(target) !== p.target_hours && Number(target) >= 1 && patch({ target_hours: Number(target) })} /></label>
        <label><span>Refresh every (days)</span>
          <input type="number" min={1} max={90} value={days} onChange={(e) => setDays(e.target.value)}
            onBlur={() => Number(days) !== p.refresh_days && Number(days) >= 1 && patch({ refresh_days: Number(days) })} /></label>
        <label><span>Rest most-aired (%)</span>
          <input type="number" min={0} max={50} value={pct} onChange={(e) => setPct(e.target.value)}
            onBlur={() => Number(pct) !== p.rotate_percent && Number(pct) >= 0 && patch({ rotate_percent: Number(pct) })} /></label>
        <label><span>Finds</span>
          <select value={p.policy} onChange={(e) => patch({ policy: e.target.value })}>
            <option value="auto_air">{lab ? "Air every find automatically (lab mode)" : "Air licensed finds automatically"}</option>
            <option value="inbox">Send everything to review</option>
          </select></label>
      </div>
      <label className="inline small" title={p.theme ? `Theme: ${p.theme}` : "This pack has no theme"}>
        <input type="checkbox" checked={p.theme_check} disabled={!p.theme_check_available || !p.theme}
          onChange={(e) => patch({ theme_check: e.target.checked })} /> AI theme check</label>
      <label className="inline small"><input type="checkbox" checked={p.enabled} onChange={(e) => patch({ enabled: e.target.checked })} /> Keep filling</label>
      {(p.rules.some((r) => r.provider === "youtube") || Object.keys(p.youtube_channels).length > 0) && <YouTubeChannels p={p} />}
      <details className="small" style={{ marginTop: 6 }}>
        <summary>{p.rules.length} rule{p.rules.length === 1 ? "" : "s"}{p.last_fill_at ? ` · last fill ${fmtTime(p.last_fill_at, meta.timezone)}` : ""}</summary>
        <ul style={{ paddingLeft: 18, margin: "6px 0" }}>
          {p.rules.map((r) => (
            <li key={r.id}>{r.name} <span className="muted">({PROVIDER[r.provider] ?? r.provider}{r.page ? `, page ${r.page}` : ""}
              {r.last_status ? `, ${r.last_status}` : ""})</span></li>
          ))}
        </ul>
      </details>
      <div className="row" style={{ marginTop: 8 }}>
        <Act run={async () => api.post(`/packs/${p.id}/fill`)} ok="Filling: new finds appear over the next few minutes.">Fill now</Act>
        <Act run={async () => api.post(`/packs/${p.id}/refresh`)} confirm="Run the weekly refresh now? It re-checks every tape, looks for new uploads and rests the most-aired."
          ok="Refreshing: the summary appears here when it finishes.">Refresh now</Act>
        <a className="btn" href={`/api/v1/packs/${p.id}/export`} download>Export</a>
        <Act className="danger" confirm={`Uninstall “${p.name}”? Its rules go; the channel, its programmes and history stay (disabled).`}
          run={async () => { await api.del(`/packs/${p.id}?disable_channel=true`); reload(); }} ok="Pack uninstalled; its channel is disabled.">Uninstall</Act>
      </div>
    </div>
  );
}

function StarterCard({ p, themeCheck, reload }: { p: PackSummary; themeCheck: boolean; reload: () => void }) {
  const [check, setCheck] = useState(false);
  return (
    <div className="panel">
      <div className="spread">
        <h3 style={{ margin: 0 }}>{p.number ? <span className="muted">{p.number} </span> : null}{p.name}</h3>
        <Chip cls={p.policy === "auto_air" ? "ok" : undefined}>{POLICY[p.policy]}</Chip>
      </div>
      <p className="small" style={{ margin: "6px 0" }}>{p.description}</p>
      <p className="small muted" style={{ margin: "0 0 8px" }}>
        {p.providers.map((x) => PROVIDER[x] ?? x).join(" + ")} · {p.target_hours} hours
        {p.blocks.length > 0 && <> · blocks: {p.blocks.join(", ")}</>}
      </p>
      <div className="row">
        {p.installed && (p.installed_version ?? 0) < p.version && (
          <Act run={async () => { await api.post("/packs/install", { starter: p.id, upgrade: true }); reload(); }}
            ok={`“${p.name}” upgraded to version ${p.version}; its channel and programmes are kept.`}>Upgrade to v{p.version}</Act>
        )}
        {p.installed ? <Chip cls="ok">installed</Chip> : (
          <Act className="primary" run={async () => {
            await api.post("/packs/install", { starter: p.id, theme_check: check });
            reload();
          }} ok={`“${p.name}” installed. The first scan is running.`}>Install</Act>
        )}
        {!p.installed && themeCheck && p.theme && (
          <label className="inline small"><input type="checkbox" checked={check} onChange={(e) => setCheck(e.target.checked)} /> theme check</label>
        )}
      </div>
    </div>
  );
}

function ImportPanel({ themeCheck, reload }: { themeCheck: boolean; reload: () => void }) {
  const notify = useToast();
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<{ spec: Record<string, unknown>; installed: boolean } | null>(null);
  const [check, setCheck] = useState(false);
  const source = (): Record<string, unknown> => {
    const t = text.trim();
    if (/^https:\/\//i.test(t)) return { url: t };
    try {
      return { spec: JSON.parse(t) };
    } catch {
      throw new Error("Paste a pack file (JSON) or an https:// address of one.");
    }
  };
  const spec = preview?.spec as { name?: string; id?: string; rules?: unknown[]; policy?: string; look?: string } | undefined;
  return (
    <div className="panel" style={{ marginTop: 16 }}>
      <h3 style={{ marginTop: 0 }}>Import a pack</h3>
      <p className="small muted">Paste a pack file or the https address of one (packs are plain JSON; export any installed pack to share it).</p>
      <textarea value={text} onChange={(e) => { setText(e.target.value); setPreview(null); }} aria-label="Pack file or URL"
        placeholder={'{"id": "my-channel", "name": "My Channel", "rules": [{"query": "…"}]}  or  https://…/pack.json'} />
      <div className="row" style={{ marginTop: 8 }}>
        <Act disabled={!text.trim()} run={async () => {
          try {
            setPreview(await api.post("/packs/preview", source()));
          } catch (e) {
            setPreview(null);
            throw e;
          }
        }}>Check</Act>
        {preview && spec && (
          <>
            <span className="small">“{spec.name}” <code>{spec.id}</code> · {spec.rules?.length ?? 0} rules · {POLICY[spec.policy ?? "inbox"]}
              {spec.look ? ` · ${spec.look} look` : ""}</span>
            {themeCheck && <label className="inline small"><input type="checkbox" checked={check} onChange={(e) => setCheck(e.target.checked)} /> theme check</label>}
            <Act className="primary" run={async () => {
              try {
                await api.post("/packs/install", { spec: preview.spec, theme_check: check, upgrade: preview.installed });
              } catch (e) {
                if (e instanceof ApiError) notify(e.message, "error");
                throw e;
              }
              setText("");
              setPreview(null);
              reload();
            }} ok="Pack installed. The first scan is running.">{preview.installed ? "Upgrade installed pack" : "Install"}</Act>
          </>
        )}
      </div>
    </div>
  );
}

function YouTubeChannels({ p }: { p: InstalledPack }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ChannelsSummary | null>(null);
  const [ref, setRef] = useState("");
  const load = async () => setData(await api.get<ChannelsSummary>(`/packs/${p.id}/channels`));
  const act = async (c: FollowedChannel, action: string) => setData(await api.post<ChannelsSummary>(`/packs/${p.id}/channels/${c.id}/${action}`));
  const counts = p.youtube_channels;
  const by = (st: FollowedChannel["status"]) => (data?.channels ?? []).filter((c) => c.status === st);
  return (
    <details className="small" style={{ marginTop: 6 }} onToggle={(e) => { const o = (e.target as HTMLDetailsElement).open; setOpen(o); if (o) load(); }}>
      <summary>YouTube channels: {counts.followed ?? 0} followed{counts.suggested ? `, ${counts.suggested} suggested` : ""}</summary>
      {open && !data && <p className="muted">Loading…</p>}
      {data && (
        <div style={{ marginTop: 6 }}>
          <p className="muted" style={{ margin: "0 0 6px" }}>Searches suggest channels; you decide. Followed channels are harvested through
            their uploads (cheap) and their free RSS feed. Searches left today: {data.searches_left_today} of {data.search_budget}.</p>
          {by("suggested").map((c) => (
            <div key={c.id} style={{ borderTop: "1px solid var(--line)", padding: "6px 0" }}>
              <div className="spread">
                <span><a href={c.url} target="_blank" rel="noreferrer">{c.title ?? c.channel_id}</a> <span className="muted">· {c.hits} find{c.hits === 1 ? "" : "s"}</span>
                  {c.ai_verdict?.fits === true && <> <Chip cls="ok" title={c.ai_verdict.reason}>AI: fits</Chip></>}
                  {c.ai_verdict?.fits === false && <> <Chip cls="warn" title={c.ai_verdict.reason}>AI: doesn't fit</Chip></>}</span>
                <span className="row">
                  <Act className="primary" run={() => act(c, "follow")} ok={`Following ${c.title ?? "channel"}.`}>Follow</Act>
                  <Act run={() => act(c, "dismiss")}>Dismiss</Act>
                  <Act run={() => act(c, "vet")} title="Ask the AI backend whether this channel fits">Ask AI</Act>
                </span>
              </div>
              {c.ai_verdict?.reason && <div className="muted">{c.ai_verdict.reason}</div>}
              {c.samples.length > 0 && <div className="muted">e.g. {c.samples.slice(-4).join(" · ")}</div>}
            </div>
          ))}
          {by("followed").map((c) => (
            <div key={c.id} className="spread" style={{ borderTop: "1px solid var(--line)", padding: "6px 0" }}>
              <span><Chip cls="ok">following</Chip> <a href={c.url} target="_blank" rel="noreferrer">{c.title ?? c.channel_id}</a>
                <span className="muted"> · {c.harvested} found{c.added_by === "admin" ? " · added by you" : ""}</span></span>
              <Act run={() => act(c, "unfollow")}>Unfollow</Act>
            </div>
          ))}
          {by("dismissed").length > 0 && (
            <p className="muted">Dismissed: {by("dismissed").map((c, i) => (
              <span key={c.id}>{i ? ", " : ""}{c.title ?? c.channel_id} <button type="button" onClick={() => act(c, "unfollow")}>restore</button></span>
            ))}</p>
          )}
          <div className="row" style={{ marginTop: 6 }}>
            <input type="text" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="@handle or youtube.com/channel/UC…"
              aria-label="YouTube channel to follow" />
            <Act disabled={!ref.trim()} run={async () => { setData(await api.post<ChannelsSummary>(`/packs/${p.id}/channels`, { ref })); setRef(""); }}
              ok="Following. Its uploads are harvested on the next fill.">Follow channel</Act>
          </div>
        </div>
      )}
    </details>
  );
}
