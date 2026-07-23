# Gophish-Support

Bulk **delete** (campaigns + userlists) and bulk **upload/verify** userbases from CSV.

## One command start

```bash
cd gophish_support
python run.py
```

Or on Linux/macOS: `chmod +x run.sh && ./run.sh`

Windows: `python run.py` or `.\run.ps1` or `python start.py`

This will:

1. Create `gophish_support/venv` and install dependencies
2. Start the web UI at **http://127.0.0.1:5000**
3. Open **Settings** in the browser to enter **Gophish URL** and **API key**

Optional: copy `.env.example` to `.env` to pre-fill credentials (the UI can save them too).

When deployed via the top-level `deploy.py`, this app runs under gunicorn as the
`gophish-support` systemd service and is reverse-proxied at **`/gophish-support/`**
(it stays subpath-aware via the `X-Forwarded-Prefix` header).

## Prerequisites

- Gophish already running (`../setup/run.sh`)
- API key from Gophish **Settings** (paste in the UI)

## CLI (optional)

```bash
source venv/bin/activate   # after first ./run.sh
export GOPHISH_API_KEY=...
python bulk_upload_userbases.py
python bulk_upload_userbases.py --dry-run
python generate_input_csvs.py   # regenerate gophish_support/input/*.csv
```

## Layout

| Path | Purpose |
|------|---------|
| `start.py` | Venv + install + launch Flask app |
| `gophish_manager.py` | Web UI (delete + upload tabs) |
| `bulk_upload_userbases.py` | CSV upload with count verification |
| `input/` | Gophish-format CSV userlists (22 files) |
| `templates/` | HTML for the manager |
