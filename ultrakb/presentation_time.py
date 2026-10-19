"""Relative display of the UTC timestamps returned by application readers."""

from datetime import datetime, timezone


def relative_time(iso_str: str) -> str:
    try:
        timestamp = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            return iso_str
    except (ValueError, TypeError, AttributeError):
        return iso_str or ""
    seconds = int((datetime.now(timezone.utc) - timestamp).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 86400 * 7:
        return f"{seconds // 86400}d ago"
    return timestamp.strftime("%Y-%m-%d")
