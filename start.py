#!/usr/bin/env python3
"""
Start the bulk manager: creates venv, installs deps, launches the web UI.

Configure Gophish URL and API key in the browser at http://127.0.0.1:5000
"""

from bootstrap import bootstrap

bootstrap()

import gophish_api  # noqa: E402

gophish_api._load_dotenv()

if gophish_api.configured():
    print(f"[*] Pre-loaded credentials from .env -> {gophish_api.GOPHISH_URL}")
else:
    print("[*] No .env credentials — set URL and API key in the web UI.")

print("[*] Gophish-Support: http://127.0.0.1:5000")
print("    Open Settings tab to connect to your Gophish server.")

from gophish_manager import app  # noqa: E402

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
