# Gophish-Support

Bulk **delete** (campaigns + userlists) and bulk **upload/verify** userbases from CSV.

## One command start

```bash
cd prod
python main.py
```

Or on Linux/macOS: `chmod +x run.sh && ./run.sh`

Windows (Python-only): `python main.py` (or `python run.py` / `python start.py`)

The launcher creates and repairs the virtual environment automatically. You do
**not** need PowerShell activation, and you do **not** need to delete anything by
hand — a venv copied from another machine is detected and rebuilt for you.

### "No Python at ...\python.exe" on Windows

This means you launched the **venv's** Python, which was copied from a different
PC and points at a Python path that does not exist on this machine. That stub
fails before any project code can run, so it cannot self-heal.

Fix: launch with a *real* Python instead of the venv stub.

1. Open a **new** terminal (so no `(venv)` prefix is shown — i.e. the venv is not
   activated).
2. Run:

```powershell
cd prod
python main.py
```

Do **not** run any of these (they use the broken copied interpreter):

- `venv\Scripts\Activate.ps1`
- `venv\Scripts\python.exe main.py`

If your editor has a "Run" button, make sure its interpreter is the system
Python, not `prod\venv`. When in doubt, run `python main.py` from a fresh
terminal. On first run it rebuilds `venv`, installs dependencies, and starts the
app at http://127.0.0.1:5000.

### "CERTIFICATE_VERIFY_FAILED" / pip cannot reach PyPI

This happens on corporate networks that inspect SSL traffic — pip cannot verify
PyPI's certificate. The launcher automatically retries with `--trusted-host`, so
usually just run `python main.py` again. If it still fails:

```powershell
# If your company uses a proxy:
$env:HTTPS_PROXY = "http://user:pass@proxy:port"
python main.py

# Or use an internal PyPI mirror provided by IT:
$env:PIP_INDEX_URL = "https://your-mirror/simple"
python main.py
```

This will:

1. Create `prod/venv` and install dependencies
2. Start the web UI at **http://127.0.0.1:5000**
3. Open **Settings** in the browser to enter **Gophish URL** and **API key**

Optional: copy `.env.example` to `.env` to pre-fill credentials (the UI can save them too).

## Prerequisites

- Gophish already running (`../setup/run.sh`)
- API key from Gophish **Settings** (paste in the prod UI)

## CLI (optional)

```bash
source venv/bin/activate   # after first ./run.sh
export GOPHISH_API_KEY=...
python bulk_upload_userbases.py
python bulk_upload_userbases.py --dry-run
python generate_input_csvs.py   # regenerate prod/input/*.csv
```

## Layout

| Path | Purpose |
|------|---------|
| `start.py` | Venv + install + launch Flask app |
| `gophish_manager.py` | Web UI (delete + upload tabs) |
| `bulk_upload_userbases.py` | CSV upload with count verification |
| `input/` | Gophish-format CSV userlists (22 files) |
| `templates/` | HTML for the manager |
