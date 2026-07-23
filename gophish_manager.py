import os

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash, session
import urllib3

import gophish_api
from bulk_upload_userbases import (
    DEFAULT_INPUT_DIR,
    list_input_csv_metadata,
    run_bulk_upload,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class _PrefixMiddleware:
    """Honor X-Forwarded-Prefix so the app works both standalone (port 5000)
    and behind an nginx reverse proxy at a subpath (e.g. /gophish-support/).

    nginx sets `proxy_set_header X-Forwarded-Prefix /gophish-support`; we copy
    it into SCRIPT_NAME so url_for() and request.script_root emit the prefix,
    while proxy_pass strips the prefix before Flask sees the path.
    """

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        prefix = environ.get("HTTP_X_FORWARDED_PREFIX", "")
        if prefix:
            environ["SCRIPT_NAME"] = "/" + prefix.strip("/")
        return self.wsgi_app(environ, start_response)


app = Flask(__name__)
app.wsgi_app = _PrefixMiddleware(app.wsgi_app)
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
        row["num_users"] = len(g.get("targets") or [])
        enriched.append(row)
    return enriched


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
        groups = gophish_api.api_get("/groups/")
        ctx["userlists"] = _enrich_userlists(groups)
    except Exception as exc:
        flash(f"Could not load data from Gophish: {exc}", "error")
        ctx["active_tab"] = "settings"

    return render_template("bulk_manager.html", **ctx)


@app.route("/settings", methods=["POST"])
def save_settings():
    url = (request.form.get("gophish_url") or "").strip().rstrip("/")
    api_key = (request.form.get("gophish_api_key") or "").strip()
    if not api_key:
        api_key = session.get("gophish_api_key") or gophish_api.API_KEY
    save_env = request.form.get("save_env") == "on"

    if not url:
        flash("Gophish URL is required.", "error")
        return redirect(url_for("index", tab="settings"))
    if not api_key:
        flash("API key is required.", "error")
        return redirect(url_for("index", tab="settings"))

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
        return redirect(url_for("index", tab="settings"))

    return redirect(url_for("index", tab="campaigns"))


@app.route("/ajax/settings", methods=["POST"])
def ajax_save_settings():
    if not request.is_json:
        return _json_error("Expected JSON body")
    data = request.get_json(silent=True) or {}
    url = (data.get("gophish_url") or "").strip().rstrip("/")
    api_key = (data.get("gophish_api_key") or "").strip()
    if not api_key:
        api_key = (session.get("gophish_api_key") or gophish_api.API_KEY or "").strip()
    if not url or not api_key:
        return _json_error("URL and API key are required.")

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


@app.route("/ajax/upload-userbases", methods=["POST"])
def ajax_upload_userbases():
    if not gophish_api.configured():
        return _json_error("Not connected to Gophish", 401)

    data = request.get_json(silent=True) or {}
    selected = data.get("csv_files") or []
    dry_run = bool(data.get("dry_run"))

    if not selected:
        return _json_error("Select at least one CSV file.")

    try:
        results, exit_code = run_bulk_upload(
            DEFAULT_INPUT_DIR,
            dry_run=dry_run,
            selected_files=selected,
        )
    except Exception as exc:
        return _json_error(str(exc), 500)

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
    return redirect(url_for("index", tab="campaigns"))


@app.route("/delete_userlists", methods=["POST"])
def delete_userlists():
    return redirect(url_for("index", tab="userlists"))


@app.route("/upload_userbases", methods=["POST"])
def upload_userbases():
    return redirect(url_for("index", tab="upload"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
