#!/usr/bin/env python3
"""
Canonical entrypoint for Gophish-Support.

Run with a normal Python interpreter (NOT an activated venv, and NOT
venv\\Scripts\\python.exe):

  python main.py

It will create/repair the virtual environment automatically and start the
web UI at http://127.0.0.1:5000
"""

from __future__ import annotations

import os
import sys


def _run_app() -> int:
    import gophish_api

    gophish_api._load_dotenv()

    if gophish_api.configured():
        print(f"[*] Pre-loaded credentials from .env -> {gophish_api.GOPHISH_URL}")
    else:
        print("[*] No .env credentials — set URL and API key in the web UI.")

    print("[*] Gophish-Support: http://127.0.0.1:5000")
    print("    Open Settings tab to connect to your Gophish server.")

    from gophish_manager import app

    app.run(host="127.0.0.1", port=5000, debug=False)
    return 0


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    os.chdir(here)
    if here not in sys.path:
        sys.path.insert(0, here)

    from bootstrap import bootstrap

    bootstrap()  # may re-exec into the venv and exit
    return _run_app()


if __name__ == "__main__":
    raise SystemExit(main())
