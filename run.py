#!/usr/bin/env python3
"""
Cross-platform launcher for Gophish-Support (same as ./run.sh).

Usage:
  python run.py
  python3 run.py

On Windows, this intentionally avoids `Activate.ps1` so you do not
need to change execution policy or trust PowerShell script publishers.
"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    start_py = os.path.join(script_dir, "start.py")
    return subprocess.call([sys.executable, start_py, *sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
