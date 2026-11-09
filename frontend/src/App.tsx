import { useEffect, useState, type FormEvent } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, ApiError, qs, type Meta, type Paged } from "./api";
import { MetaContext } from "./ui";
import Dashboard from "./pages/Dashboard";
import Discovery from "./pages/Discovery";
import Review from "./pages/Review";
import Catalogue from "./pages/Catalogue";
import Item from "./pages/Item";
import Channels from "./pages/Channels";
import ChannelPage from "./pages/Channel";
import Schedule from "./pages/Schedule";
import Catchup from "./pages/Catchup";
import Sources from "./pages/Sources";
import Settings from "./pages/Settings";

// Everyday tasks first; the technical pages sit in a quieter second group.
const NAV: [string, string][] = [
  ["/channels", "Channels"], ["/review", "Inbox"], ["/discovery", "Find videos"], ["/catalogue", "Library"],
];
const NAV_MORE: [string, string][] = [
  ["/status", "Status"], ["/schedule", "Schedule"], ["/catchup", "Catch-up"], ["/sources", "Playback sources"], ["/settings", "Settings"],
];

/** Number of items waiting in the inbox, refreshed every 30 s. */
function useInboxCount(enabled: boolean): number | null {
  const [n, setN] = useState<number | null>(null);
  useEffect(() => {
    if (!enabled) return;
    const load = () => api.get<Paged<unknown>>(`/catalogue${qs({ status: "PENDING_REVIEW", per_page: 1 })}`)
      .then((r) => setN(r.total)).catch(() => {});
    load();
    const h = setInterval(load, 30000);
    window.addEventListener("atv:inbox-changed", load);
    return () => { clearInterval(h); window.removeEventListener("atv:inbox-changed", load); };
  }, [enabled]);
  return n;
}

export default function App() {
  const [user, setUser] = useState<string | null | undefined>(undefined);
  const [meta, setMeta] = useState<Meta | null>(null);

  useEffect(() => {
    api.get<{ user: string }>("/auth/me").then((r) => setUser(r.user)).catch(() => setUser(null));
    const out = () => setUser(null);
    window.addEventListener("atv:unauthorised", out);
    return () => window.removeEventListener("atv:unauthorised", out);
  }, []);
  useEffect(() => {
    if (user) api.get<Meta>("/meta").then(setMeta).catch(() => setMeta(null));
  }, [user]);

  const inbox = useInboxCount(!!user);
  if (user === undefined) return null;
  if (user === null) return <Login onLogin={setUser} />;
  if (!meta) return <p className="muted" style={{ padding: 20 }}>Loading…</p>;

  return (
    <MetaContext.Provider value={meta}>
      <header className="top">
        <NavLink className="brand" to="/">ArchiveTV</NavLink>
        <nav>
          {NAV.map(([to, name]) => (
            <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "on" : "")}>
              {name}{to === "/review" && !!inbox && <span className="badge">{inbox}</span>}
            </NavLink>
          ))}
          <span className="nav-more">
            {NAV_MORE.map(([to, name]) => (
              <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "on" : "")}>{name}</NavLink>
            ))}
          </span>
        </nav>
        <span className="small muted">{user}</span>
        <button onClick={async () => { await api.post("/auth/logout"); setUser(null); }}>Sign out</button>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/channels" replace />} />
          <Route path="/status" element={<Dashboard />} />
          <Route path="/discovery" element={<Discovery />} />
          <Route path="/review" element={<Review />} />
          <Route path="/catalogue" element={<Catalogue />} />
          <Route path="/items/:id" element={<Item />} />
          <Route path="/channels" element={<Channels />} />
          <Route path="/channels/:id" element={<ChannelPage />} />
          <Route path="/schedule" element={<Schedule />} />
          <Route path="/catchup" element={<Catchup />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </MetaContext.Provider>
  );
}

function Login({ onLogin }: { onLogin: (u: string) => void }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const r = await api.post<{ user: string }>("/auth/login", { username, password });
      onLogin(r.user);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "sign-in failed");
    }
  };
  return (
    <div className="panel login">
      <h1>ArchiveTV Manager</h1>
      {error && <div className="flash error">{error}</div>}
      <form onSubmit={submit}>
        <label>Username<br /><input type="text" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required /></label>
        <label>Password<br /><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required autoFocus /></label>
        <p><button className="primary">Sign in</button></p>
      </form>
    </div>
  );
}
