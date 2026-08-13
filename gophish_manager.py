import os
import json
import time

from flask import Flask, Response, jsonify, render_template, request, redirect, url_for, flash, session, stream_with_context
import urllib3

import gophish_api
from bulk_upload_userbases import (
    DEFAULT_INPUT_DIR,
    estimate_upload_eta,
    list_input_csv_metadata,
    run_bulk_upload,
    run_bulk_upload_iter,
)
from bulk_create_campaigns import fetch_create_resources, run_bulk_create, run_bulk_create_iter

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ------------------------------------------------------------------
# Server-side resource cache (avoids hammering Gophish on every tab
# switch). TTL = 30 s; invalidated by ?refresh=1 or server restart.
# ------------------------------------------------------------------
_RESOURCE_CACHE: dict = {"data": None, "ts": 0.0, "key": None}
_RESOURCE_CACHE_TTL = 30  # seconds


def _resource_cache_key() -> tuple:
    """Identity of the Gophish server the cache belongs to.

    Without this, switching the URL/API key (e.g. from localhost to the real
    server) could serve the previous server's groups/templates/pages/SMTP for
    up to the TTL, causing campaign creation to POST names that don't exist on
    the new server — a 400 "not found" that only happens after switching.
    """
    return (gophish_api.GOPHISH_URL or "", gophish_api.API_KEY or "")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    "gophish-bulk-manager-change-in-production",
)


def _sync_session_to_api() -> None:
    url = session.get("gophish_url")
    key = session.get("gophish_api_key")
    if url or key:
        gophish_api.apply_credentials(
            url=url or gophish_api.GOPHISH_URL,
            api_key=key or "",
        )


def _save_credentials_to_env_file(url: str, api_key: str) -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    preserved: dict[str, str] = {}
    if os.path.isfile(env_path):
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key not in ("GOPHISH_URL", "GOPHISH_API_KEY", "FLASK_SECRET_KEY"):
                    preserved[key] = val.strip()
    lines = [
        f"GOPHISH_URL={url}",
        f"GOPHISH_API_KEY={api_key}",
        f"FLASK_SECRET_KEY={app.secret_key}",
    ]
    for key in sorted(preserved):
        lines.append(f"{key}={preserved[key]}")
    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    gophish_api._load_dotenv()


def _mask_key(key: str) -> str:
    if not key or len(key) < 12:
        return "••••••••"
    return key[:6] + "…" + key[-4:]


def _enrich_userlists(groups: list) -> list:
    enriched = []
    for g in groups:
        row = dict(g)
        # Support both /groups/ (full targets list) and /groups/summary (num_targets)
        if "num_targets" in g:
            row["num_users"] = int(g["num_targets"] or 0)
        else:
            row["num_users"] = len(g.get("targets") or [])
        enriched.append(row)
    return enriched


def _fetch_groups_for_index() -> list:
    """Fetch groups using lightweight summary endpoint; fall back to full list."""
    try:
        data = gophish_api.api_get("/groups/summary")
        if isinstance(data, dict) and "groups" in data:
            return data["groups"] if isinstance(data["groups"], list) else []
        if isinstance(data, list):
            return data
    except Exception:
        pass
    data = gophish_api.api_get("/groups/")
    return data if isinstance(data, list) else []


@app.before_request
def load_credentials():
    if session.get("gophish_url") or session.get("gophish_api_key"):
        _sync_session_to_api()
    elif gophish_api.configured():
        session.setdefault("gophish_url", gophish_api.GOPHISH_URL)
        session.setdefault("gophish_api_key", gophish_api.API_KEY)


def _context_base():
    configured = gophish_api.configured()
    return {
        "configured": configured,
        "gophish_url": session.get("gophish_url") or gophish_api.GOPHISH_URL,
        "api_key_masked": _mask_key(session.get("gophish_api_key") or gophish_api.API_KEY),
        "input_dir": DEFAULT_INPUT_DIR,
        "input_files": list_input_csv_metadata() if configured else [],
    }


def _json_error(message: str, status: int = 400):
    return jsonify({"ok": False, "message": message}), status


@app.route("/", methods=["GET"])
def index():
    ctx = _context_base()
    ctx["campaigns"] = []
    ctx["userlists"] = []
    ctx["active_tab"] = request.args.get(
        "tab", "settings" if not ctx["configured"] else "campaigns"
    )

    if not ctx["configured"]:
        return render_template("bulk_manager.html", **ctx)

    try:
        data = gophish_api.api_get("/campaigns/summary")
        ctx["campaigns"] = data.get("campaigns", [])
        groups = _fetch_groups_for_index()
        ctx["userlists"] = _enrich_userlists(groups)
    except Exception as exc:
        flash(gophish_api.format_api_error(exc), "error")
        ctx["active_tab"] = "settings"

    return render_template("bulk_manager.html", **ctx)


@app.route("/settings", methods=["POST"])
def save_settings():
    url = gophish_api.normalize_gophish_url(request.form.get("gophish_url") or "")
    api_key = (request.form.get("gophish_api_key") or "").strip()
    if not api_key:
        api_key = session.get("gophish_api_key") or gophish_api.API_KEY
    save_env = request.form.get("save_env") == "on"

    ok_url, url_msg = gophish_api.validate_gophish_url(url)
    if not ok_url:
        flash(url_msg, "error")
        return redirect("/?tab=settings")
    if not api_key:
        flash("API key is required.", "error")
        return redirect("/?tab=settings")

    session["gophish_url"] = url
    session["gophish_api_key"] = api_key
    gophish_api.apply_credentials(url=url, api_key=api_key)

    ok, msg = gophish_api.test_connection()
    if ok:
        flash(msg, "success")
        if save_env:
            _save_credentials_to_env_file(url, api_key)
            flash("Saved to prod/.env for next run.", "success")
    else:
        flash(msg, "error")
        return redirect("/?tab=settings")

    return redirect("/?tab=campaigns")


@app.route("/ajax/settings", methods=["POST"])
def ajax_save_settings():
    if not request.is_json:
        return _json_error("Expected JSON body")
    data = request.get_json(silent=True) or {}
    url = gophish_api.normalize_gophish_url(data.get("gophish_url") or "")
    api_key = (data.get("gophish_api_key") or "").strip()
    if not api_key:
        api_key = (session.get("gophish_api_key") or gophish_api.API_KEY or "").strip()
    ok_url, url_msg = gophish_api.validate_gophish_url(url)
    if not ok_url:
        return _json_error(url_msg)
    if not api_key:
        return _json_error("API key is required.")

    session["gophish_url"] = url
    session["gophish_api_key"] = api_key
    gophish_api.apply_credentials(url=url, api_key=api_key)
    ok, msg = gophish_api.test_connection()
    if data.get("save_env"):
        _save_credentials_to_env_file(url, api_key)
    return jsonify({"ok": ok, "message": msg})


@app.route("/ajax/delete-campaigns", methods=["POST"])
def ajax_delete_campaigns():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)

    data = request.get_json(silent=True) or {}
    ids = data.get("campaign_ids") or []
    if not ids:
        return _json_error("No campaigns selected.")

    deleted, failed = 0, []
    total = len(ids)
    for i, cid in enumerate(ids, start=1):
        try:
            gophish_api.api_delete(f"/campaigns/{cid}")
            deleted += 1
        except Exception as exc:
            failed.append({"id": cid, "error": str(exc)})

    ok = len(failed) == 0
    return jsonify(
        {
            "ok": ok,
            "message": f"Deleted {deleted} of {total} campaign(s).",
            "deleted": deleted,
            "total": total,
            "failed": failed,
        }
    )


@app.route("/ajax/delete-userlists", methods=["POST"])
def ajax_delete_userlists():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)

    data = request.get_json(silent=True) or {}
    ids = data.get("userlist_ids") or []
    if not ids:
        return _json_error("No userlists selected.")

    deleted, failed = 0, []
    total = len(ids)
    for gid in ids:
        try:
            gophish_api.api_delete(f"/groups/{gid}")
            deleted += 1
        except Exception as exc:
            failed.append({"id": gid, "error": str(exc)})

    ok = len(failed) == 0
    return jsonify(
        {
            "ok": ok,
            "message": f"Deleted {deleted} of {total} userlist(s).",
            "deleted": deleted,
            "total": total,
            "failed": failed,
        }
    )


@app.route("/ajax/campaign-create/resources", methods=["GET"])
def ajax_campaign_create_resources():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)

    force_refresh = request.args.get("refresh") == "1"
    now = time.time()
    cache = _RESOURCE_CACHE
    cache_key = _resource_cache_key()
    if (
        not force_refresh
        and cache["data"] is not None
        and cache.get("key") == cache_key
        and (now - cache["ts"]) < _RESOURCE_CACHE_TTL
    ):
        return jsonify({"ok": True, "cached": True, **cache["data"]})

    try:
        data = fetch_create_resources()
    except Exception as exc:
        return _json_error(gophish_api.format_api_error(exc), 500)

    cache["data"] = data
    cache["ts"] = time.time()
    cache["key"] = cache_key
    return jsonify({"ok": True, "cached": False, **data})


@app.route("/ajax/campaign-create/create", methods=["POST"])
def ajax_campaign_create_bulk():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)

    data = request.get_json(silent=True) or {}
    items = data.get("items") or []
    smtp_name = (data.get("smtp_name") or "").strip()
    phishing_url = (data.get("phishing_url") or "http://localhost").strip()
    recheck = data.get("recheck", True)
    use_stream = bool(data.get("stream"))

    if use_stream:

        def generate():
            try:
                for evt in run_bulk_create_iter(
                    items,
                    smtp_name=smtp_name,
                    phishing_url=phishing_url,
                    recheck=recheck,
                ):
                    yield json.dumps(evt) + "\n"
            except ValueError as exc:
                yield json.dumps({"type": "error", "message": str(exc)}) + "\n"
            except Exception as exc:
                yield json.dumps(
                    {"type": "error", "message": gophish_api.format_api_error(exc)}
                ) + "\n"

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
        )

    try:
        results, exit_code = run_bulk_create(
            items,
            smtp_name=smtp_name,
            phishing_url=phishing_url,
            recheck=recheck,
        )
    except ValueError as exc:
        return _json_error(str(exc))
    except Exception as exc:
        return _json_error(gophish_api.format_api_error(exc), 500)

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    return jsonify(
        {
            "ok": exit_code == 0,
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
    )


@app.route("/ajax/upload-userbases/estimate", methods=["POST"])
def ajax_upload_estimate():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)
    data = request.get_json(silent=True) or {}
    selected = data.get("csv_files") or []
    if not selected:
        return _json_error("Select at least one CSV file.")
    recheck = data.get("recheck", True)
    try:
        eta = estimate_upload_eta(DEFAULT_INPUT_DIR, selected, recheck=recheck)
    except Exception as exc:
        return _json_error(str(exc), 500)
    return jsonify({"ok": True, **eta})


@app.route("/ajax/upload-userbases", methods=["POST"])
def ajax_upload_userbases():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)

    data = request.get_json(silent=True) or {}
    selected = data.get("csv_files") or []
    dry_run = bool(data.get("dry_run"))
    recheck = data.get("recheck", True) and not dry_run
    use_stream = bool(data.get("stream"))

    if not selected:
        return _json_error("Select at least one CSV file.")

    if use_stream:

        def generate():
            try:
                for evt in run_bulk_upload_iter(
                    DEFAULT_INPUT_DIR,
                    dry_run=dry_run,
                    selected_files=selected,
                    recheck=recheck,
                ):
                    yield json.dumps(evt) + "\n"
            except Exception as exc:
                yield json.dumps(
                    {"type": "error", "message": gophish_api.format_api_error(exc)}
                ) + "\n"

        return Response(
            stream_with_context(generate()),
            mimetype="application/x-ndjson",
        )

    try:
        results, exit_code = run_bulk_upload(
            DEFAULT_INPUT_DIR,
            dry_run=dry_run,
            selected_files=selected,
            recheck=recheck,
        )
    except Exception as exc:
        return _json_error(gophish_api.format_api_error(exc), 500)

    passed = sum(1 for r in results if r.ok)
    total = len(results)
    return jsonify(
        {
            "ok": exit_code == 0,
            "message": f"Verified {passed}/{total} file(s).",
            "passed": passed,
            "total": total,
            "results": [
                {
                    "file": r.csv_file,
                    "group": r.group_name,
                    "ok": r.ok,
                    "local": r.local_count,
                    "import": r.import_count,
                    "stored": r.stored_count,
                    "errors": r.errors,
                }
                for r in results
            ],
        }
    )


# Legacy form POST fallbacks (redirect)
@app.route("/delete", methods=["POST"])
def delete_campaigns():
    return redirect("/?tab=campaigns")


@app.route("/delete_userlists", methods=["POST"])
def delete_userlists():
    return redirect("/?tab=userlists")


@app.route("/upload_userbases", methods=["POST"])
def upload_userbases():
    return redirect("/?tab=upload")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
