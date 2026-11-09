// Thin client for the ArchiveTV /api/v1 REST API. Auth is the admin session
// cookie (same origin), so no tokens are handled in the browser.

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type Json = Record<string, unknown> | unknown[] | null;

async function request<T>(method: string, path: string, body?: Json | FormData): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const res = await fetch(`/api/v1${path}`, init);
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    const msg = typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : res.statusText;
    if (res.status === 401 && path !== "/auth/login") window.dispatchEvent(new Event("atv:unauthorised"));
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: Json | FormData) => request<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body: Json) => request<T>("PATCH", path, body),
  put: <T>(path: string, body: Json) => request<T>("PUT", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
};

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Source types that can feed IPTV (and so carry rights and validation). */
export const IPTV_SOURCE_TYPES = ["direct_url", "local_file", "youtube_stream"];

// ---------------------------------------------------------------- shapes

export interface Meta {
  review_statuses: string[];
  eligibility: string[];
  rights: string[];
  playback_types: string[];
  roles: string[];
  modes: string[];
  formats: string[];
  rule_kinds: string[];
  providers: Record<string, string>;
  retention_choices: number[];
  timezone: string;
  schedule_days: number;
  feed_token: boolean;
  feed_query: string;
  minimal_disk: boolean;
  youtube_streaming: boolean;
}

export interface MediaSummary {
  id: number;
  provider: string;
  external_id: string;
  url: string | null;
  title: string;
  creator: string | null;
  duration: number | null;
  publication_date: string | null;
  thumbnail_url: string | null;
  review_status: string;
  eligibility: string;
  schedulable: boolean;
  iptv_status: string;
  duplicate_of_id: number | null;
  flags: string[];
  updated_at: string | null;
}

export interface Source {
  id: number;
  type: string;
  url: string | null;
  local_path: string | null;
  label: string | null;
  eligibility: string;
  rights_status: string;
  rights_note?: string | null;
  license_url?: string | null;
  health_ok: boolean;
  seekable: boolean | null;
  live?: boolean;
  video_codec: string | null;
  audio_codec: string | null;
  width: number | null;
  height: number | null;
  duration: number | null;
  size_bytes?: number | null;
  last_validated_at: string | null;
  last_error: string | null;
  priority?: number;
  allow_cache?: boolean | null;
  media_item_id?: number;
  item_title?: string | null;
  checksum?: string | null;
}

export interface MediaDetail extends MediaSummary {
  description: string | null;
  tags: string[];
  categories: string[];
  series: string | null;
  season: number | null;
  episode: number | null;
  restrictions: Record<string, unknown>;
  review_note: string | null;
  reviewed_at: string | null;
  channels: { channel_id: number; role: string }[];
  playback_sources: Source[];
}

export interface Entry {
  id: number;
  start: string;
  end: string;
  start_local: string;
  kind: string;
  title: string;
  media_item_id: number | null;
  playback_source_id: number | null;
  block: string | null;
  overridden: boolean;
}

export interface Channel {
  id: number;
  slug: string;
  name: string;
  tvg_id: string;
  number: number | null;
  description: string | null;
  logo_url: string | null;
  group_title: string;
  enabled: boolean;
  mode: string;
  seed: string;
  repeat_avoid_count: number;
  playback_format: string;
  fallback_item_id: number | null;
  catchup_enabled: boolean;
  catchup_days: number;
  items: number;
  now_playing: string | null;
}

export interface ChannelDetail extends Omit<Channel, "items"> {
  idents_between: boolean;
  filler_pad: boolean;
  logo_file: string | null;
  logo: string | null;
  rule_ids: number[];
  items: { id: number; media_item_id: number; title: string | null; role: string; position: number; duration: number | null;
    review_status: string | null; schedulable: boolean; problem: string | null; provider: string | null;
    thumbnail_url: string | null; live: boolean }[];
  blocks: { id: number; name: string; days: string; start_time: string; end_time: string; tag_filter: string | null; mode: string }[];
}

export interface Rule {
  id: number;
  name: string;
  provider: string;
  kind: string;
  query: string | null;
  params: Record<string, unknown>;
  scan_interval_minutes: number;
  max_results: number;
  enabled: boolean;
  last_run_at: string | null;
  last_status: string | null;
  last_error: string | null;
  last_new_count: number;
  channel_ids: number[];
}

export interface Paged<T> {
  total: number;
  page: number;
  pages: number;
  items: T[];
}

/** Outcome of putting one item on air (approve + authorise + add to channel). */
export interface AirResult {
  id: number;
  title: string;
  on_air: boolean;
  problem: string | null;
}

export interface LinkResult {
  url: string;
  items: AirResult[];
  error: string | null;
}
