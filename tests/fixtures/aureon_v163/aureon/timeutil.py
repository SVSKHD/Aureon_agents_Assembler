"""Timezone-aware UTC everywhere; naive values only at the DB/MT5 boundary, always meaning UTC."""
import datetime as dt
UTC = dt.timezone.utc
def now(): return dt.datetime.now(UTC)
def naive(x):
    """Any datetime/str -> naive UTC datetime."""
    if isinstance(x, str): x = dt.datetime.fromisoformat(x)
    if x.tzinfo is not None: x = x.astimezone(UTC).replace(tzinfo=None)
    return x
def from_epoch(sec, offset_hours=0.0):
    """MT5 epoch (server clock stamped as if UTC) -> naive UTC."""
    return dt.datetime.fromtimestamp(sec, UTC).replace(tzinfo=None) - dt.timedelta(hours=offset_hours)
