"""
Bulk-upload Gophish userlists from prod/input/ CSV files and verify counts.

For each CSV:
  1. Count rows locally (UTF-8)
  2. Import via /api/import/group (Gophish CSV parser — catches encoding issues)
  3. Create group via POST /api/groups/ (name = CSV filename without .csv)
  4. Download group via GET /api/groups/{id} and compare user counts + emails

Usage:
  set GOPHISH_API_KEY=your-key
  python bulk_upload_userbases.py
  python bulk_upload_userbases.py --input-dir ./input --dry-run
  python bulk_upload_userbases.py --verify-only
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional, Set, Tuple

import gophish_api
import gophish_bulk_config as bulk_cfg

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_INPUT_DIR = os.path.join(SCRIPT_DIR, "input")


@dataclass
class UploadResult:
    csv_file: str
    group_name: str
    local_count: int
    import_count: int
    stored_count: int
    group_id: Optional[int] = None
    ok: bool = False
    errors: List[str] = field(default_factory=list)
    missing_emails: List[str] = field(default_factory=list)
    extra_emails: List[str] = field(default_factory=list)


def list_csv_files(input_dir: str) -> List[str]:
    if not os.path.isdir(input_dir):
        raise FileNotFoundError(f"Input directory not found: {input_dir}")
    files = sorted(
        f
        for f in os.listdir(input_dir)
        if f.lower().endswith(".csv")
    )
    if not files:
        raise FileNotFoundError(f"No CSV files in {input_dir}")
    return [os.path.join(input_dir, f) for f in files]


def count_csv_rows_local(csv_path: str) -> Tuple[int, Set[str]]:
    """Parse CSV locally with UTF-8; return row count and normalized emails."""
    emails: Set[str] = set()
    count = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            email = (row.get("Email") or row.get("email") or "").strip()
            if not email:
                continue
            count += 1
            emails.add(email.lower())
    return count, emails


def normalize_targets(targets: List[dict]) -> Set[str]:
    return {
        (t.get("email") or "").strip().lower()
        for t in targets
        if (t.get("email") or "").strip()
    }


def group_name_for_csv(csv_path: str) -> str:
    """Gophish group name = CSV basename without extension (no prefix)."""
    return os.path.splitext(os.path.basename(csv_path))[0]


ProgressCallback = Callable[[Dict[str, Any]], None]


def _emit(on_progress: Optional[ProgressCallback], event: Dict[str, Any]) -> None:
    if on_progress:
        on_progress(event)


def upload_result_dict(r: UploadResult) -> dict:
    return {
        "file": r.csv_file,
        "group": r.group_name,
        "ok": r.ok,
        "local": r.local_count,
        "import": r.import_count,
        "stored": r.stored_count,
        "errors": r.errors,
        "recheck": getattr(r, "recheck", False),
    }


def process_csv(
    csv_path: str,
    *,
    dry_run: bool,
    on_progress: Optional[ProgressCallback] = None,
) -> UploadResult:
    name = group_name_for_csv(csv_path)
    local_count, local_emails = count_csv_rows_local(csv_path)
    result = UploadResult(
        csv_file=os.path.basename(csv_path),
        group_name=name,
        local_count=local_count,
        import_count=0,
        stored_count=0,
    )

    if local_count == 0:
        result.errors.append("CSV has no valid email rows")
        return result

    if dry_run:
        result.import_count = local_count
        result.stored_count = local_count
        result.ok = True
        return result

    def tick(phase: str) -> None:
        _emit(on_progress, {"type": "phase", "file": result.csv_file, "phase": phase})

    try:
        tick("import")
        imported = gophish_api.import_group_csv(csv_path, expected_rows=local_count)
    except Exception as exc:
        result.errors.append(f"API import failed: {gophish_api.format_api_error(exc)}")
        return result

    gophish_api.cooldown(on_tick=lambda s, r: _emit(on_progress, {"type": "cooldown", "seconds": s, "reason": r}))

    result.import_count = len(imported)
    if result.import_count != local_count:
        result.errors.append(
            f"Import count mismatch: local={local_count} api_import={result.import_count}"
        )

    import_emails = normalize_targets(imported)
    missing_at_import = sorted(local_emails - import_emails)
    if missing_at_import:
        result.errors.append(
            f"{len(missing_at_import)} email(s) lost during API import (encoding/parser)"
        )
        result.missing_emails = missing_at_import[:10]

    try:
        tick("create_group")
        group = gophish_api.create_group(name, imported)
    except Exception as exc:
        result.errors.append(f"Create group failed: {gophish_api.format_api_error(exc)}")
        return result

    gophish_api.cooldown(on_tick=lambda s, r: _emit(on_progress, {"type": "cooldown", "seconds": s, "reason": r}))

    result.group_id = group.get("id")
    if not result.group_id:
        result.errors.append("Group created but no id returned")
        return result

    try:
        tick("verify")
        downloaded = gophish_api.get_group(
            result.group_id, target_count=result.import_count
        )
    except Exception as exc:
        result.errors.append(f"Download group failed: {gophish_api.format_api_error(exc)}")
        return result

    gophish_api.cooldown(on_tick=lambda s, r: _emit(on_progress, {"type": "cooldown", "seconds": s, "reason": r}))

    stored = downloaded.get("targets") or []
    result.stored_count = len(stored)
    stored_emails = normalize_targets(stored)

    if result.stored_count != result.import_count:
        result.errors.append(
            f"Stored count mismatch: imported={result.import_count} stored={result.stored_count}"
        )

    missing_stored = sorted(import_emails - stored_emails)
    extra_stored = sorted(stored_emails - import_emails)
    if missing_stored:
        result.missing_emails.extend(missing_stored[:10])
        result.errors.append(f"{len(missing_stored)} email(s) missing after save")
    if extra_stored:
        result.extra_emails = extra_stored[:10]
        result.errors.append(f"{len(extra_stored)} unexpected email(s) in stored group")

    result.ok = (
        result.import_count == local_count
        and result.stored_count == result.import_count
        and not missing_stored
        and not missing_at_import
    )
    return result


def verify_existing_groups(input_dir: str = DEFAULT_INPUT_DIR) -> List[UploadResult]:
    """Re-download groups whose names match CSV stems in input_dir."""
    try:
        csv_stems = {
            os.path.splitext(os.path.basename(p))[0]
            for p in list_csv_files(input_dir)
        }
    except FileNotFoundError:
        return []

    groups = gophish_api.api_get("/groups/")
    results: List[UploadResult] = []
    for g in groups:
        name = g.get("name") or ""
        if name not in csv_stems:
            continue
        gid = g.get("id")
        full = gophish_api.get_group(gid)
        targets = full.get("targets") or []
        results.append(
            UploadResult(
                csv_file="(existing)",
                group_name=name,
                local_count=-1,
                import_count=len(targets),
                stored_count=len(targets),
                group_id=gid,
                ok=True,
            )
        )
    return results


def print_report(results: List[UploadResult]) -> int:
    ok_count = sum(1 for r in results if r.ok)
    print(f"\n{'=' * 72}")
    print(f"Gophish: {gophish_api.GOPHISH_URL}")
    print(f"Results: {ok_count}/{len(results)} passed")
    print(f"{'=' * 72}")
    for r in results:
        status = "PASS" if r.ok else "FAIL"
        print(f"\n[{status}] {r.csv_file} -> {r.group_name}")
        if r.group_id:
            print(f"       group id: {r.group_id}")
        print(
            f"       counts: local={r.local_count}  import={r.import_count}  stored={r.stored_count}"
        )
        for err in r.errors:
            print(f"       ! {err}")
        if r.missing_emails:
            print(f"       missing sample: {r.missing_emails[:5]}")
    return 0 if ok_count == len(results) else 1


def list_input_csv_metadata(input_dir: str = DEFAULT_INPUT_DIR) -> List[dict]:
    """Return sorted metadata for each CSV in input_dir."""
    if not os.path.isdir(input_dir):
        return []
    out = []
    for name in sorted(os.listdir(input_dir)):
        if not name.lower().endswith(".csv"):
            continue
        path = os.path.join(input_dir, name)
        count, _ = count_csv_rows_local(path)
        out.append({"name": name, "users": count, "path": path})
    return out


def resolve_csv_paths(input_dir: str, selected_names: List[str]) -> List[str]:
    """Map selected basenames to absolute paths (no path traversal)."""
    if not selected_names:
        return []
    allowed = {
        n
        for n in os.listdir(input_dir)
        if n.lower().endswith(".csv") and os.path.isfile(os.path.join(input_dir, n))
    }
    paths = []
    for name in selected_names:
        base = os.path.basename(name)
        if base in allowed:
            paths.append(os.path.join(input_dir, base))
    return sorted(paths)


def estimate_upload_eta(
    input_dir: str,
    selected_files: List[str],
    *,
    recheck: bool = True,
) -> dict:
    paths = resolve_csv_paths(input_dir, selected_files)
    if not paths:
        return {"eta_seconds": 0, "eta_label": "0s", "files": 0}
    seconds = bulk_cfg.estimate_upload_total_seconds(
        paths, count_csv_rows_local, recheck=recheck
    )
    return {
        "eta_seconds": int(seconds),
        "eta_label": bulk_cfg.format_eta(seconds),
        "files": len(paths),
        "cooldown_sec": bulk_cfg.COOLDOWN_SEC,
        "cooldown_final_sec": bulk_cfg.COOLDOWN_FINAL_SEC,
        "bulk_timeout_max": bulk_cfg.BULK_TIMEOUT_MAX,
    }


def run_bulk_upload_iter(
    input_dir: str = DEFAULT_INPUT_DIR,
    *,
    dry_run: bool = False,
    selected_files: Optional[List[str]] = None,
    recheck: bool = True,
) -> Generator[Dict[str, Any], None, None]:
    """Yield progress events (NDJSON-friendly) including recheck pass and final cooldown."""
    if not gophish_api.configured() and not dry_run:
        yield {"type": "error", "message": "Not connected to Gophish."}
        return

    if selected_files:
        csv_files = resolve_csv_paths(input_dir, selected_files)
        if not csv_files:
            yield {"type": "error", "message": "No valid CSV files selected."}
            return
    else:
        csv_files = list_csv_files(input_dir)

    total = len(csv_files)
    eta = bulk_cfg.estimate_upload_total_seconds(
        csv_files, count_csv_rows_local, recheck=recheck and not dry_run
    )
    yield {
        "type": "start",
        "total": total,
        "eta_seconds": int(eta),
        "eta_label": bulk_cfg.format_eta(eta),
        "dry_run": dry_run,
        "recheck": recheck and not dry_run,
        "group_naming": "csv_stem",
    }

    results: List[UploadResult] = []
    started = __import__("time").time()

    for idx, path in enumerate(csv_files, start=1):
        elapsed = __import__("time").time() - started
        remaining = max(0, eta - elapsed)
        yield {
            "type": "progress",
            "index": idx,
            "total": total,
            "file": os.path.basename(path),
            "eta_seconds": int(remaining),
            "eta_label": bulk_cfg.format_eta(remaining),
        }

        file_events: List[Dict[str, Any]] = []

        def capture(ev: Dict[str, Any]) -> None:
            ev = dict(ev)
            ev.setdefault("index", idx)
            ev.setdefault("total", total)
            file_events.append(ev)

        result = process_csv(
            path,
            dry_run=dry_run,
            on_progress=capture,
        )
        for ev in file_events:
            yield ev
        results.append(result)
        yield {"type": "file_done", "result": upload_result_dict(result)}

    failed = [r for r in results if not r.ok]
    if recheck and not dry_run and failed:
        yield {
            "type": "recheck_start",
            "count": len(failed),
            "cooldown_seconds": bulk_cfg.COOLDOWN_FINAL_SEC,
            "message": f"Rechecking {len(failed)} failed file(s) after server cooldown",
        }
        yield {
            "type": "cooldown",
            "seconds": bulk_cfg.COOLDOWN_FINAL_SEC,
            "reason": "recheck cooldown — letting Gophish recover",
        }
        bulk_cfg.cooldown_final()
        for r in failed:
            path = os.path.join(input_dir, r.csv_file)
            if not os.path.isfile(path):
                continue
            setattr(r, "recheck", True)
            retry = process_csv(path, dry_run=False)
            retry.recheck = True
            for i, old in enumerate(results):
                if old.csv_file == retry.csv_file:
                    results[i] = retry
                    break
            yield {"type": "file_done", "result": upload_result_dict(retry), "recheck": True}

    if not dry_run:
        yield {
            "type": "cooldown",
            "seconds": bulk_cfg.COOLDOWN_FINAL_SEC,
            "reason": "final server cooldown",
        }
        bulk_cfg.cooldown_final()

    passed = sum(1 for r in results if r.ok)
    yield {
        "type": "complete",
        "ok": passed == len(results),
        "message": f"Verified {passed}/{len(results)} file(s).",
        "passed": passed,
        "total": len(results),
        "results": [upload_result_dict(r) for r in results],
    }


def run_bulk_upload(
    input_dir: str = DEFAULT_INPUT_DIR,
    *,
    dry_run: bool = False,
    verify_only: bool = False,
    selected_files: Optional[List[str]] = None,
    recheck: bool = True,
) -> Tuple[List[UploadResult], int]:
    if not gophish_api.configured() and not dry_run:
        raise SystemExit(
            "Gophish is not configured. Set URL and API key in the prod UI or .env."
        )

    if verify_only:
        results = verify_existing_groups(input_dir)
        if not results:
            print("No groups found with names matching CSV files in input dir")
            return [], 1
        return results, print_report(results)

    if selected_files:
        csv_files = resolve_csv_paths(input_dir, selected_files)
        if not csv_files:
            raise FileNotFoundError("No valid CSV files selected.")
    else:
        csv_files = list_csv_files(input_dir)
    print(f"Uploading {len(csv_files)} CSV file(s) from {input_dir}")
    if dry_run:
        print("(dry-run: no API writes)")

    results: List[UploadResult] = []
    for event in run_bulk_upload_iter(
        input_dir,
        dry_run=dry_run,
        selected_files=selected_files,
        recheck=recheck,
    ):
        if event.get("type") == "complete":
            return [
                UploadResult(
                    csv_file=r["file"],
                    group_name=r["group"],
                    local_count=r["local"],
                    import_count=r["import"],
                    stored_count=r["stored"],
                    ok=r["ok"],
                    errors=r.get("errors") or [],
                )
                for r in event.get("results") or []
            ], 0 if event.get("ok") else 1
    return results, 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk upload Gophish userlists from CSV and verify user counts."
    )
    parser.add_argument(
        "--input-dir",
        default=DEFAULT_INPUT_DIR,
        help=f"Directory of CSV files (default: {DEFAULT_INPUT_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only parse local CSVs, do not call Gophish API",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Re-fetch groups whose names match CSV filenames in input dir",
    )
    args = parser.parse_args()
    _, code = run_bulk_upload(
        args.input_dir,
        dry_run=args.dry_run,
        verify_only=args.verify_only,
    )
    sys.exit(code)


if __name__ == "__main__":
    main()
