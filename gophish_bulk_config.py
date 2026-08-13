"""Shared timing, cooldown, and ETA settings for bulk Gophish operations."""

from __future__ import annotations

import os
import time
from typing import Callable, Optional, TypeVar

T = TypeVar("T")

DEFAULT_API_TIMEOUT = 30
DEFAULT_BULK_TIMEOUT = 1200  # 20 minutes base for large imports
DEFAULT_BULK_TIMEOUT_MAX = 3600  # 1 hour cap
DEFAULT_BULK_PER_USER_SEC = 0.75  # added to timeout per target row
DEFAULT_COOLDOWN_SEC = 5  # pause between API steps within one file
DEFAULT_COOLDOWN_FINAL_SEC = 20  # pause before recheck / after batch
DEFAULT_API_RETRIES = 3
DEFAULT_RETRY_DELAY_SEC = 12


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def reload_bulk_config() -> None:
    global API_TIMEOUT, BULK_TIMEOUT, BULK_TIMEOUT_MAX, BULK_PER_USER_SEC
    global COOLDOWN_SEC, COOLDOWN_FINAL_SEC, API_RETRIES, RETRY_DELAY_SEC
    API_TIMEOUT = _int_env("GOPHISH_API_TIMEOUT", DEFAULT_API_TIMEOUT)
    BULK_TIMEOUT = _int_env("GOPHISH_BULK_TIMEOUT", DEFAULT_BULK_TIMEOUT)
    BULK_TIMEOUT_MAX = _int_env("GOPHISH_BULK_TIMEOUT_MAX", DEFAULT_BULK_TIMEOUT_MAX)
    BULK_PER_USER_SEC = _float_env("GOPHISH_BULK_PER_USER_SEC", DEFAULT_BULK_PER_USER_SEC)
    COOLDOWN_SEC = _float_env("GOPHISH_COOLDOWN_SEC", DEFAULT_COOLDOWN_SEC)
    COOLDOWN_FINAL_SEC = _float_env("GOPHISH_COOLDOWN_FINAL_SEC", DEFAULT_COOLDOWN_FINAL_SEC)
    API_RETRIES = _int_env("GOPHISH_API_RETRIES", DEFAULT_API_RETRIES)
    RETRY_DELAY_SEC = _float_env("GOPHISH_RETRY_DELAY_SEC", DEFAULT_RETRY_DELAY_SEC)


API_TIMEOUT = DEFAULT_API_TIMEOUT
BULK_TIMEOUT = DEFAULT_BULK_TIMEOUT
BULK_TIMEOUT_MAX = DEFAULT_BULK_TIMEOUT_MAX
BULK_PER_USER_SEC = DEFAULT_BULK_PER_USER_SEC
COOLDOWN_SEC = DEFAULT_COOLDOWN_SEC
COOLDOWN_FINAL_SEC = DEFAULT_COOLDOWN_FINAL_SEC
API_RETRIES = DEFAULT_API_RETRIES
RETRY_DELAY_SEC = DEFAULT_RETRY_DELAY_SEC

reload_bulk_config()


def request_timeout(target_count: Optional[int] = None) -> int:
    """Scale bulk timeouts with row count; routine calls stay short."""
    if target_count is None:
        return API_TIMEOUT
    extra = int(max(0, target_count) * BULK_PER_USER_SEC)
    return min(BULK_TIMEOUT + extra, BULK_TIMEOUT_MAX)


def cooldown(seconds: Optional[float] = None, *, on_tick: Optional[Callable[[float, str], None]] = None, reason: str = "") -> None:
    """Let Gophish cool down between heavy API calls."""
    wait = COOLDOWN_SEC if seconds is None else seconds
    if wait <= 0:
        return
    label = reason or "cooldown"
    if on_tick:
        on_tick(wait, label)
    time.sleep(wait)


def cooldown_final(*, on_tick: Optional[Callable[[float, str], None]] = None, reason: str = "final cooldown") -> None:
    cooldown(COOLDOWN_FINAL_SEC, on_tick=on_tick, reason=reason)


def format_eta(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"~{s}s"
    mins, sec = divmod(s, 60)
    if mins < 60:
        return f"~{mins}m {sec}s" if sec else f"~{mins}m"
    hours, mins = divmod(mins, 60)
    return f"~{hours}h {mins}m"


def estimate_per_csv_seconds(local_count: int, *, include_recheck: bool = False) -> float:
    """Rough seconds per CSV (import + create + verify + cooldowns)."""
    steps_cooldown = COOLDOWN_SEC * 3
    api_time = 30 + local_count * 0.4 + request_timeout(local_count) * 0.05
    total = steps_cooldown + api_time
    if include_recheck:
        total = total * 1.35 + COOLDOWN_FINAL_SEC
    return total


def estimate_upload_total_seconds(csv_paths: list, count_fn, *, recheck: bool = True) -> float:
    total = 0.0
    for path in csv_paths:
        try:
            n, _ = count_fn(path)
        except Exception:
            n = 100
        total += estimate_per_csv_seconds(n, include_recheck=False)
    if recheck:
        total += COOLDOWN_FINAL_SEC + total * 0.2
    else:
        total += COOLDOWN_FINAL_SEC * 0.5
    return total
