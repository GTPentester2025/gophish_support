"""Shared Gophish REST API helpers."""

import os
from typing import Any, Dict, List, Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_URL = "https://127.0.0.1:3333"
DEFAULT_TIMEOUT = 30
DEFAULT_BULK_TIMEOUT = 600  # 10 minutes for large group import/create/download
GOPHISH_URL = DEFAULT_URL
API_KEY = ""
API_TIMEOUT = DEFAULT_TIMEOUT
BULK_TIMEOUT = DEFAULT_BULK_TIMEOUT


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
    global GOPHISH_URL, API_KEY, API_TIMEOUT, BULK_TIMEOUT
    GOPHISH_URL = os.environ.get("GOPHISH_URL", DEFAULT_URL)
    API_KEY = os.environ.get("GOPHISH_API_KEY", "")
    try:
        API_TIMEOUT = int(os.environ.get("GOPHISH_API_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        API_TIMEOUT = DEFAULT_TIMEOUT
    try:
        BULK_TIMEOUT = int(os.environ.get("GOPHISH_BULK_TIMEOUT", DEFAULT_BULK_TIMEOUT))
    except ValueError:
        BULK_TIMEOUT = DEFAULT_BULK_TIMEOUT


def request_timeout(target_count: Optional[int] = None) -> int:
    """Routine API calls stay short; bulk uploads use GOPHISH_BULK_TIMEOUT."""
    if target_count is None:
        return API_TIMEOUT
    return BULK_TIMEOUT


def apply_credentials(url: Optional[str] = None, api_key: Optional[str] = None) -> None:
    """Set URL/key in the environment and refresh module globals."""
    if url is not None:
        os.environ["GOPHISH_URL"] = url.rstrip("/")
    if api_key is not None:
        os.environ["GOPHISH_API_KEY"] = api_key.strip()
    _reload_config()


def configured() -> bool:
    return bool(API_KEY and GOPHISH_URL)


def test_connection() -> tuple[bool, str]:
    """Return (ok, message) for the current credentials."""
    if not API_KEY:
        return False, "API key is required."
    if not GOPHISH_URL:
        return False, "Gophish URL is required."
    try:
        api_get("/campaigns/summary")
        return True, "Connected successfully."
    except requests.exceptions.ConnectionError:
        return False, f"Cannot reach server at {GOPHISH_URL}"
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            return False, "Invalid API key or insufficient permissions."
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def api_get(path: str, **kwargs) -> Any:
    params = kwargs.pop("params", {})
    params["api_key"] = API_KEY
    url = f"{GOPHISH_URL.rstrip('/')}/api{path}"
    timeout = kwargs.pop("timeout", API_TIMEOUT)
    resp = requests.get(url, params=params, verify=False, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def api_post(path: str, json: Optional[dict] = None, **kwargs) -> Any:
    params = kwargs.pop("params", {})
    params["api_key"] = API_KEY
    url = f"{GOPHISH_URL.rstrip('/')}/api{path}"
    timeout = kwargs.pop("timeout", API_TIMEOUT)
    resp = requests.post(url, json=json, params=params, verify=False, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def api_delete(path: str, **kwargs) -> Any:
    params = kwargs.pop("params", {})
    params["api_key"] = API_KEY
    url = f"{GOPHISH_URL.rstrip('/')}/api{path}"
    timeout = kwargs.pop("timeout", API_TIMEOUT)
    resp = requests.delete(url, params=params, verify=False, timeout=timeout, **kwargs)
    resp.raise_for_status()
    return resp.json()


def import_group_csv(
    csv_path: str, *, expected_rows: Optional[int] = None
) -> List[Dict[str, str]]:
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


def create_group(name: str, targets: List[Dict[str, str]]) -> Dict[str, Any]:
    return api_post(
        "/groups/",
        json={"name": name, "targets": targets},
        timeout=request_timeout(len(targets)),
    )


def get_group(group_id: int, *, target_count: Optional[int] = None) -> Dict[str, Any]:
    return api_get(
        f"/groups/{group_id}",
        timeout=request_timeout(target_count),
    )


_load_dotenv()
