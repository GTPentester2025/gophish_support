"""Bulk campaign creation helpers for the Gophish workspace UI."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Generator, List, Optional, Set

import gophish_api
import gophish_bulk_config as bulk_cfg


def default_campaign_name(group_name: str, when: Optional[datetime] = None) -> str:
    """PROD prefix + group name + date (editable in UI)."""
    dt = when or datetime.now(timezone.utc)
    return f"PROD {group_name} {dt.strftime('%Y-%m-%d')}"


def summarize_groups(groups: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = []
    for g in groups:
        # /groups/summary returns num_targets; /groups/ returns targets list
        num = g.get("num_targets")
        if num is None:
            num = len(g.get("targets") or [])
        rows.append(
            {
                "id": g.get("id"),
                "name": g.get("name") or "",
                "num_users": int(num),
                "modified_date": g.get("modified_date") or "",
            }
        )
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return rows


def summarize_named(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = [{"id": x.get("id"), "name": x.get("name") or ""} for x in items]
    rows.sort(key=lambda r: (r["name"] or "").lower())
    return rows


def _fetch_groups_safe() -> List[Dict[str, Any]]:
    """Fetch groups using the lightweight summary endpoint; fall back to full list."""
    try:
        data = gophish_api.api_get("/groups/summary")
        # summary endpoint returns {"total": N, "groups": [...]}
        if isinstance(data, dict) and "groups" in data:
            return data["groups"] if isinstance(data["groups"], list) else []
        # Some Gophish versions return the list directly even from /summary
        if isinstance(data, list):
            return data
    except Exception:
        pass
    # Fall back to full group list (heavier but always available)
    data = gophish_api.api_get("/groups/")
    return data if isinstance(data, list) else []


def fetch_create_resources() -> Dict[str, Any]:
    """Fetch groups, templates, pages and SMTP profiles in parallel."""
    tasks: Dict[str, Callable] = {
        "groups": _fetch_groups_safe,
        "templates": lambda: gophish_api.api_get("/templates/"),
        "pages": lambda: gophish_api.api_get("/pages/"),
        "smtp_profiles": lambda: gophish_api.api_get("/smtp/"),
    }

    results: Dict[str, Any] = {}
    errors: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                errors[key] = str(exc)
                results[key] = []

    if errors:
        # Surface first error so the UI can show a meaningful message
        first_key = next(iter(errors))
        raise RuntimeError(f"Failed to load {first_key}: {errors[first_key]}")

    groups_raw = results["groups"] if isinstance(results["groups"], list) else []
    templates_raw = results["templates"] if isinstance(results["templates"], list) else []
    pages_raw = results["pages"] if isinstance(results["pages"], list) else []
    smtp_raw = results["smtp_profiles"] if isinstance(results["smtp_profiles"], list) else []

    return {
        "groups": summarize_groups(groups_raw),
        "templates": summarize_named(templates_raw),
        "pages": summarize_named(pages_raw),
        "smtp_profiles": summarize_named(smtp_raw),
        "default_campaign_name": default_campaign_name("Example Group"),
    }


def _fetch_existing_campaign_names() -> Set[str]:
    """Return the set of campaign names already in Gophish (lowercase for comparison)."""
    try:
        data = gophish_api.api_get("/campaigns/summary")
        campaigns = data.get("campaigns", []) if isinstance(data, dict) else []
        return {(c.get("name") or "").strip().lower() for c in campaigns}
    except Exception:
        return set()


@dataclass
class CreateRowResult:
    campaign_name: str
    group_name: str
    ok: bool
    campaign_id: Optional[int] = None
    errors: List[str] = field(default_factory=list)


def _launch_date_iso() -> str:
    """Canonical RFC3339 UTC timestamp ending in 'Z'.

    This matches exactly what Gophish's own web UI sends (e.g.
    ``2026-06-17T13:46:00Z``). Some Gophish/Go builds are stricter than others
    about the trailing offset, and ``Z`` is the form every version accepts, so
    we avoid the ``+00:00`` variant to stay compatible across servers.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def create_campaign_payload(
    *,
    campaign_name: str,
    group_name: str,
    template_name: str,
    page_name: str,
    smtp_name: str,
    phishing_url: str,
) -> Dict[str, Any]:
    return {
        "name": campaign_name.strip(),
        "template": {"name": template_name},
        "page": {"name": page_name},
        "smtp": {"name": smtp_name},
        "url": phishing_url.strip() or "http://localhost",
        "launch_date": _launch_date_iso(),
        "groups": [{"name": group_name}],
    }


def run_bulk_create_iter(
    items: List[Dict[str, Any]],
    *,
    smtp_name: str = "",
    phishing_url: str,
    recheck: bool = True,
) -> Generator[Dict[str, Any], None, None]:
    if not items:
        yield {"type": "error", "message": "Workspace is empty — add at least one user group."}
        return

    total = len(items)
    eta = total * (bulk_cfg.COOLDOWN_SEC + 25)
    if recheck:
        eta += bulk_cfg.COOLDOWN_FINAL_SEC + total * 10
    yield {
        "type": "start",
        "total": total,
        "eta_seconds": int(eta),
        "eta_label": bulk_cfg.format_eta(eta),
        "recheck": recheck,
    }

    # Fetch existing campaign names once to guard against duplicates.
    existing_names = _fetch_existing_campaign_names()

    # Build in-batch duplicate map (lower-cased campaign name -> first occurrence index)
    batch_names: Dict[str, int] = {}
    for idx, raw in enumerate(items):
        grp = (raw.get("group_name") or "").strip()
        cname = ((raw.get("campaign_name") or "").strip() or default_campaign_name(grp)).lower()
        if cname not in batch_names:
            batch_names[cname] = idx

    results: List[CreateRowResult] = []
    started = __import__("time").time()

    def create_one(raw: Dict[str, Any], *, skip_existing: bool = True) -> CreateRowResult:
        group_name = (raw.get("group_name") or "").strip()
        campaign_name = (raw.get("campaign_name") or "").strip()
        template_name = (raw.get("template_name") or "").strip()
        page_name = (raw.get("page_name") or "").strip()
        row_smtp = (raw.get("smtp_name") or smtp_name or "").strip()

        if not group_name:
            return CreateRowResult(
                campaign_name=campaign_name or "(unnamed)",
                group_name="",
                ok=False,
                errors=["Missing user group."],
            )
        if not campaign_name:
            campaign_name = default_campaign_name(group_name)
        if not template_name or not page_name:
            return CreateRowResult(
                campaign_name=campaign_name,
                group_name=group_name,
                ok=False,
                errors=["Template and landing page are required."],
            )
        if not row_smtp:
            return CreateRowResult(
                campaign_name=campaign_name,
                group_name=group_name,
                ok=False,
                errors=["Sending profile (SMTP) is required for this row."],
            )

        # Duplicate guard: skip if a campaign with this name already exists in Gophish.
        if skip_existing and campaign_name.lower() in existing_names:
            return CreateRowResult(
                campaign_name=campaign_name,
                group_name=group_name,
                ok=False,
                errors=[
                    f'Campaign "{campaign_name}" already exists in Gophish. '
                    "Rename it before creating."
                ],
            )

        payload = create_campaign_payload(
            campaign_name=campaign_name,
            group_name=group_name,
            template_name=template_name,
            page_name=page_name,
            smtp_name=row_smtp,
            phishing_url=phishing_url,
        )
        try:
            created = gophish_api.call_with_retry(
                lambda: gophish_api.api_post(
                    "/campaigns/", json=payload, timeout=bulk_cfg.request_timeout()
                )
            )
            cid = created.get("id") if isinstance(created, dict) else None
            # Add to existing_names so subsequent duplicate checks within this batch work.
            existing_names.add(campaign_name.lower())
            return CreateRowResult(
                campaign_name=campaign_name,
                group_name=group_name,
                ok=True,
                campaign_id=cid,
            )
        except Exception as exc:
            return CreateRowResult(
                campaign_name=campaign_name,
                group_name=group_name,
                ok=False,
                errors=[gophish_api.format_api_error(exc)],
            )

    for idx, raw in enumerate(items, start=1):
        elapsed = __import__("time").time() - started
        remaining = max(0, eta - elapsed)
        grp = (raw.get("group_name") or "").strip()
        cname = (raw.get("campaign_name") or "").strip() or default_campaign_name(grp)

        # Check for in-batch duplicate (this row is a later occurrence).
        cname_lower = cname.lower()
        is_inbatch_dup = batch_names.get(cname_lower, idx - 1) < (idx - 1)

        yield {
            "type": "progress",
            "index": idx,
            "total": total,
            "campaign_name": cname,
            "eta_seconds": int(remaining),
            "eta_label": bulk_cfg.format_eta(remaining),
        }

        if is_inbatch_dup:
            row = CreateRowResult(
                campaign_name=cname,
                group_name=grp,
                ok=False,
                errors=["Duplicate campaign name in this batch — rename before creating."],
            )
        else:
            row = create_one(raw)

        results.append(row)
        yield {
            "type": "row_done",
            "result": {
                "campaign_name": row.campaign_name,
                "group_name": row.group_name,
                "ok": row.ok,
                "campaign_id": row.campaign_id,
                "errors": row.errors,
            },
        }
        gophish_api.cooldown()

    failed_indices = [i for i, r in enumerate(results) if not r.ok]
    if recheck and failed_indices:
        yield {
            "type": "recheck_start",
            "count": len(failed_indices),
            "cooldown_seconds": bulk_cfg.COOLDOWN_FINAL_SEC,
        }
        bulk_cfg.cooldown_final()
        yield {
            "type": "cooldown",
            "seconds": bulk_cfg.COOLDOWN_FINAL_SEC,
            "reason": "recheck cooldown",
        }
        for i in failed_indices:
            raw = items[i]
            grp = (raw.get("group_name") or "").strip()
            cname = (raw.get("campaign_name") or "").strip() or default_campaign_name(grp)
            # Safe recheck: only retry if the campaign does NOT already exist
            # (the first attempt may have succeeded right before timing out).
            if cname.lower() in existing_names:
                row = CreateRowResult(
                    campaign_name=cname,
                    group_name=grp,
                    ok=True,
                    errors=[],
                )
                row.errors = []
            else:
                row = create_one(raw, skip_existing=False)
            results[i] = row
            yield {
                "type": "row_done",
                "result": {
                    "campaign_name": row.campaign_name,
                    "group_name": row.group_name,
                    "ok": row.ok,
                    "campaign_id": row.campaign_id,
                    "errors": row.errors,
                },
                "recheck": True,
            }
            gophish_api.cooldown()

    bulk_cfg.cooldown_final()
    yield {
        "type": "cooldown",
        "seconds": bulk_cfg.COOLDOWN_FINAL_SEC,
        "reason": "final server cooldown",
    }

    passed = sum(1 for r in results if r.ok)
    yield {
        "type": "complete",
        "ok": passed == total,
        "message": f"Created {passed} of {total} campaign(s).",
        "passed": passed,
        "total": total,
        "results": [
            {
                "campaign_name": r.campaign_name,
                "group_name": r.group_name,
                "ok": r.ok,
                "campaign_id": r.campaign_id,
                "errors": r.errors,
            }
            for r in results
        ],
    }


def run_bulk_create(
    items: List[Dict[str, Any]],
    *,
    smtp_name: str = "",
    phishing_url: str,
    recheck: bool = True,
) -> tuple[List[CreateRowResult], int]:
    """Create campaigns; returns (results, exit_code). Per-row smtp_name overrides global default."""
    results: List[CreateRowResult] = []
    exit_code = 1
    for event in run_bulk_create_iter(
        items, smtp_name=smtp_name, phishing_url=phishing_url, recheck=recheck
    ):
        if event.get("type") == "complete":
            exit_code = 0 if event.get("ok") else 1
            results = [
                CreateRowResult(
                    campaign_name=r["campaign_name"],
                    group_name=r["group_name"],
                    ok=r["ok"],
                    campaign_id=r.get("campaign_id"),
                    errors=r.get("errors") or [],
                )
                for r in event.get("results") or []
            ]
        elif event.get("type") == "error":
            raise ValueError(event.get("message", "Create failed"))
    return results, exit_code
