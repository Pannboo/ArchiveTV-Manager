"""Provider status and API quota accounting (persisted)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .db import session_scope, utcnow
from .models import ProviderStatus

# YouTube Data API quotas reset at midnight Pacific time.
PACIFIC = ZoneInfo("America/Los_Angeles")


def _quota_day(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(PACIFIC).strftime("%Y-%m-%d")


def _next_reset_utc() -> datetime:
    now_pt = datetime.now(timezone.utc).astimezone(PACIFIC)
    nxt = (now_pt + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return nxt.astimezone(timezone.utc).replace(tzinfo=None)


class QuotaTracker:
    def __init__(self, provider: str, daily_limit: int, reserve: int = 0):
        self.provider = provider
        self.daily_limit = daily_limit
        self.reserve = reserve

    def _row(self, s) -> ProviderStatus:
        row = s.get(ProviderStatus, self.provider)
        if row is None:
            row = ProviderStatus(provider=self.provider, quota_used=0, quota_day=_quota_day())
            s.add(row)
        if row.quota_day != _quota_day():
            row.quota_day = _quota_day()
            row.quota_used = 0
            row.quota_exhausted_until = None
        return row

    def can_spend(self, units: int, use_reserve: bool = False) -> bool:
        with session_scope() as s:
            row = self._row(s)
            if row.quota_exhausted_until and row.quota_exhausted_until > utcnow():
                return False
            limit = self.daily_limit - (0 if use_reserve else self.reserve)
            return row.quota_used + units <= limit

    def spend(self, units: int) -> None:
        with session_scope() as s:
            self._row(s).quota_used += units

    def mark_exhausted(self) -> None:
        with session_scope() as s:
            row = self._row(s)
            row.quota_exhausted_until = _next_reset_utc()
            row.quota_used = max(row.quota_used, self.daily_limit)

    def used(self) -> int:
        with session_scope() as s:
            return self._row(s).quota_used


def record_success(provider: str) -> None:
    with session_scope() as s:
        row = s.get(ProviderStatus, provider) or ProviderStatus(provider=provider, quota_used=0)
        row.last_success_at = utcnow()
        s.add(row)


def record_error(provider: str, message: str) -> None:
    with session_scope() as s:
        row = s.get(ProviderStatus, provider) or ProviderStatus(provider=provider, quota_used=0)
        row.last_error_at = utcnow()
        row.last_error = message[:2000]
        s.add(row)
