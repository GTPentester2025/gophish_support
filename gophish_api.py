"""Shared Gophish REST API helpers."""

import os
import time
from typing import Any, Callable, Dict, List, Optional, TypeVar

import requests
import urllib3

import gophish_bulk_config as bulk_cfg

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_URL = "https://127.0.0.1:3333"
GOPHISH_URL = DEFAULT_URL
API_KEY = ""

T = TypeVar("T")


def _load_dotenv() -> None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    _reload_config()


def _reload_config() -> None:
    global GOPHISH_URL, API_KEY
    GOPHISH_URL = os.environ.get("GOPHISH_URL", DEFAULT_URL)
    API_KEY = os.environ.get("GOPHISH_API_KEY", "")
    bulk_cfg.reload_bulk_config()


def request_timeout(target_count: Optional[int] = None) -> int:
    return bulk_cfg.request_timeout(target_count)


def cooldown(
    seconds: Optional[float] = None,
    *,
    on_tick: Optional[Callable[[float, str], None]] = None,
    reason: str = "",
) -> None:
    bulk_cfg.cooldown(seconds, on_tick=on_tick, reason=reason)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in (502, 503, 504, 429)
    return False


def call_with_retry(
    fn: Callable[[], T],
    *,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
) -> T:
    """Retry transient failures with delay so Gophish can recover."""
    last: Optional[Exception] = None
    attempts = max(1, bulk_cfg.API_RETRIES)
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                raise
            wait = bulk_cfg.RETRY_DELAY_SEC * attempt
            if on_retry:
                on_retry(attempt, exc, wait)
            time.sleep(wait)
    assert last is not None
    raise last


def apply_credentials(url: Optional[str] = None, api_key: Optional[str] = None) -> None:
    """Set URL/key in the environment and refresh module globals."""
    if url is not None:
        os.environ["GOPHISH_URL"] = normalize_gophish_url(url)
    if api_key is not None:
        os.environ["GOPHISH_API_KEY"] = api_key.strip()
    _reload_config()


def configured() -> bool:
    return bool(API_KEY and GOPHISH_URL)


def normalize_gophish_url(url: str) -> str:
    """Strip and ensure scheme. Supports local and remote hosts with a port."""
    url = (url or "").strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def validate_gophish_url(url: str) -> tuple[bool, str]:
    """Basic check: https?://host[:port] — local or remote."""
    from urllib.parse import urlparse

    if not url:
        return False, "Gophish URL is required."
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            False,
            "URL must start with https:// — e.g. https://127.0.0.1:3333 or "
            "https://your-server.com:3333",
        )
    if not parsed.hostname:
        return (
            False,
            "URL must include a host — e.g. https://127.0.0.1:3333 or "
            "https://your-server.com:3333",
        )
    return True, ""


def extract_server_message(resp: "requests.Response | None") -> str:
    """Pull Gophish's own error text out of a response body.

    Gophish replies to rejected requests with ``{"success": false,
    "message": "Template not found"}``. Surfacing that message is the only
    reliable way to tell *why* a campaign POST returned 400 (template / page /
    sending profile / group not found, invalid JSON, duplicate, etc.).
    """
    if resp is None:
        return ""
    try:
        data = resp.json()
    except ValueError:
        text = (resp.text or "").strip()
        # Avoid dumping a whole HTML error page into the UI.
        if text and "<html" not in text.lower() and len(text) < 300:
            return text
        return ""
    if isinstance(data, dict):
        msg = data.get("message") or data.get("error") or ""
        return str(msg).strip()
    return ""


def format_api_error(exc: Exception) -> str:
    """Short, actionable message for UI (avoids dumping full urllib3 traces)."""
    raw = str(exc)
    refused = (
        "10061" in raw
        or "actively refused" in raw.lower()
        or "connection refused" in raw.lower()
        or "failed to establish a new connection" in raw.lower()
    )
    if refused or isinstance(exc, requests.exceptions.ConnectionError):
        host = GOPHISH_URL or "(no URL set)"
        local = "127.0.0.1" in host or "localhost" in host.lower()
        if local:
            return (
                f"Cannot connect to {host}. For local Gophish, start the server on this PC "
                "first. For a remote server, open Settings and use "
                "https://your-hostname:port (same URL as Gophish admin in your browser)."
            )
        return (
            f"Cannot connect to {host}. Check the host, port, and that Gophish is running. "
            "For local use try https://127.0.0.1:3333 (with Gophish started on this PC)."
        )
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        server_msg = extract_server_message(exc.response)
        if status in (401, 403):
            return server_msg or "Invalid API key or insufficient permissions."
        if status == 404:
            return (
                server_msg
                or "Gophish returned 404 — the API path was not found. Confirm the "
                "Gophish URL/port and that this is the admin server (default :3333)."
            )
        # 400 and everything else: show the exact reason Gophish gave us.
        if server_msg:
            return f"Gophish rejected the request (HTTP {status}): {server_msg}"
        return f"Gophish returned HTTP {status} with no detail. Raw: {raw.split(chr(10))[0][:300]}"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"Timed out talking to {GOPHISH_URL}. Try again or increase GOPHISH_API_TIMEOUT in .env."
    return raw.split("\n")[0][:500]


def test_connection() -> tuple[bool, str]:
    """Return (ok, message) for the current credentials."""
    if not API_KEY:
        return False, "API key is required."
    if not GOPHISH_URL:
        return False, "Gophish URL is required."
    try:
        api_get("/campaigns/summary")
        return True, "Connected successfully."
    except requests.exceptions.ConnectionError as exc:
        return False, format_api_error(exc)
    except requests.exceptions.HTTPError as exc:
        return False, format_api_error(exc)
    except Exception as exc:
        return False, format_api_error(exc)


def _auth_headers(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Send the API key as a Bearer token *and* as a query param.

    Different Gophish releases expect different things: older builds only read
    the ``?api_key=`` query parameter, while newer builds (and most reverse
    proxies) accept the ``Authorization: Bearer`` header. Sending both keeps the
    tool compatible across versions and deployments without any config.
    """
    headers = {"Authorization": f"Bearer {API_KEY}"}
    if extra:
        headers.update(extra)
    return headers


def api_get(path: str, **kwargs) -> Any:
    params = kwargs.pop("params", {})
    params["api_key"] = API_KEY
    headers = _auth_headers(kwargs.pop("headers", None))
    url = f"{GOPHISH_URL.rstrip('/')}/api{path}"
    timeout = kwargs.pop("timeout", bulk_cfg.API_TIMEOUT)
    resp = requests.get(url, params=params, headers=headers, verify=False, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, json: Optional[dict] = None, **kwargs) -> Any:
    params = kwargs.pop("params", {})
    params["api_key"] = API_KEY
    headers = _auth_headers(kwargs.pop("headers", None))
    url = f"{GOPHISH_URL.rstrip('/')}/api{path}"
    timeout = kwargs.pop("timeout", bulk_cfg.API_TIMEOUT)
    resp = requests.post(url, json=json, params=params, headers=headers, verify=False, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def api_delete(path: str, **kwargs) -> Any:
    params = kwargs.pop("params", {})
    params["api_key"] = API_KEY
    headers = _auth_headers(kwargs.pop("headers", None))
    url = f"{GOPHISH_URL.rstrip('/')}/api{path}"
    timeout = kwargs.pop("timeout", bulk_cfg.API_TIMEOUT)
    resp = requests.delete(url, params=params, headers=headers, verify=False, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def import_group_csv(
    csv_path: str, *, expected_rows: Optional[int] = None
) -> List[Dict[str, str]]:
    def _do() -> List[Dict[str, str]]:
        url = f"{GOPHISH_URL.rstrip('/')}/api/import/group"
        headers = {"Authorization": f"Bearer {API_KEY}"}
        with open(csv_path, "rb") as fh:
            files = {"files[]": (os.path.basename(csv_path), fh, "text/csv")}
            resp = requests.post(
                url,
                files=files,
                params={"api_key": API_KEY},
                headers=headers,
                verify=False,
                timeout=request_timeout(expected_rows or 100),
            )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and data.get("success") is False:
            raise RuntimeError(data.get("message", "CSV import failed"))
        return data

    return call_with_retry(_do)


def create_group(name: str, targets: List[Dict[str, str]]) -> Dict[str, Any]:
    return call_with_retry(
        lambda: api_post(
            "/groups/",
            json={"name": name, "targets": targets},
            timeout=request_timeout(len(targets)),
        )
    )


def get_group(group_id: int, *, target_count: Optional[int] = None) -> Dict[str, Any]:
    return call_with_retry(
        lambda: api_get(
            f"/groups/{group_id}",
            timeout=request_timeout(target_count),
        )
    )


_load_dotenv()
