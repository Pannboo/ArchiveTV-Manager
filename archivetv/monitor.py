"""Resource and storage monitoring.

Reports what the minimal-disk architecture promises to keep bounded: the
SQLite database, temporary filesystem, RAM stream buffers, FFmpeg processes
and container memory. Alerts are raised when a configured limit is crossed
and are shown on the dashboard, in /health and (on change) via the webhook.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .config import settings

_last_cpu: tuple[float, float] | None = None  # (wall, cpu seconds incl. children)
_active_alerts: set[str] = set()


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _rss_bytes(pid: int | str) -> int:
    txt = _read(f"/proc/{pid}/status") or ""
    for line in txt.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


def _dir_bytes(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def memory() -> dict:
    """Container memory (cgroup v2 when available) and this process's RSS."""
    used = _read("/sys/fs/cgroup/memory.current")
    limit = _read("/sys/fs/cgroup/memory.max")
    d: dict = {"process_rss": _rss_bytes("self")}
    if used and used.isdigit():
        d["cgroup_used"] = int(used)
    if limit and limit.isdigit():
        d["cgroup_limit"] = int(limit)
    if "cgroup_used" in d and "cgroup_limit" in d:
        d["percent"] = round(100 * d["cgroup_used"] / d["cgroup_limit"], 1)
    return d


def cpu_percent() -> float | None:
    """CPU used by the app and its FFmpeg children since the previous call,
    as a percentage of one core."""
    global _last_cpu
    t = os.times()
    now, used = time.monotonic(), t.user + t.system + t.children_user + t.children_system
    prev, _last_cpu = _last_cpu, (now, used)
    if prev is None or now <= prev[0]:
        return None
    return round(100 * (used - prev[1]) / (now - prev[0]), 1)


def stream_buffers(manager) -> dict:
    """Bytes currently held in RAM: per-viewer queues plus keyframe caches."""
    from .stream import READ_SIZE

    queued = gop = viewers = 0
    for st in list(manager.streams.values()) + list(manager.catchups.values()):
        if st.finished:
            continue
        gop += st.gop_bytes
        for q in st.viewers:
            viewers += 1
            queued += q.qsize() * READ_SIZE
    return {"queued_bytes": queued, "keyframe_cache_bytes": gop, "viewers": viewers}


def ffmpeg_rss(manager) -> int:
    return sum(_rss_bytes(p) for p in list(manager._pids))


def snapshot(manager) -> dict:
    db = settings.db_path
    db_bytes = sum(p.stat().st_size for p in (db, db.with_name(db.name + "-wal")) if p.exists())
    # Not tempfile.gettempdir(): it probes by writing a file, which fails on
    # exactly the full filesystem this check exists to report.
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    tmp_usage = shutil.disk_usage(tmp)
    data_usage = shutil.disk_usage(settings.data_dir)
    snap = {
        "database_bytes": db_bytes,
        "database_limit_bytes": settings.db_max_mb * 1024 * 1024,
        "backups_bytes": _dir_bytes(settings.data_dir / "backups"),
        "cache_bytes": _dir_bytes(settings.cache_dir) if settings.cache_dir.exists() else 0,
        "data_volume_free_bytes": data_usage.free,
        "tmp": {"path": str(tmp), "used_bytes": tmp_usage.used, "total_bytes": tmp_usage.total,
                "percent": round(100 * tmp_usage.used / tmp_usage.total, 1) if tmp_usage.total else 0},
        "memory": memory(),
        "cpu_percent": cpu_percent(),
        "buffers": stream_buffers(manager),
        "active_streams": len(manager.status()),
        "catchup_sessions": len(manager.catchup_status()),
        "ffmpeg_processes": manager.ffmpeg_processes(),
        "ffmpeg_rss_bytes": ffmpeg_rss(manager),
        "minimal_disk": settings.minimal_disk,
        "cache_active": settings.cache_active,
    }
    snap["alerts"] = alerts(snap)
    return snap


def alerts(snap: dict) -> list[str]:
    out = []
    if snap["database_bytes"] > snap["database_limit_bytes"]:
        out.append(f"database is {snap['database_bytes'] / 2**20:.0f} MB (limit {settings.db_max_mb} MB)")
    if snap["tmp"]["percent"] >= settings.tmp_warn_percent:
        out.append(f"temporary filesystem {snap['tmp']['percent']}% full")
    mem = snap["memory"].get("percent")
    if mem is not None and mem >= settings.mem_warn_percent:
        out.append(f"container memory at {mem}% of its limit")
    if snap["data_volume_free_bytes"] < 512 * 2**20:
        out.append("less than 512 MB free on the data volume")
    if snap["cache_bytes"] and settings.minimal_disk:
        out.append("media found in the cache directory while minimal-disk mode is on")
    if snap["ffmpeg_processes"] > settings.max_streams + settings.max_catchup_sessions:
        out.append(f"{snap['ffmpeg_processes']} FFmpeg processes running (expected at most "
                   f"{settings.max_streams + settings.max_catchup_sessions})")
    return out


def new_alerts(current: list[str]) -> list[str]:
    """Alerts that were not active at the previous check (for notifications)."""
    global _active_alerts
    keys = {a.split(" ")[0] + a.split(" ")[1] if " " in a else a for a in current}
    fresh = [a for a, k in zip(current, keys) if k not in _active_alerts]
    _active_alerts = keys
    return fresh
