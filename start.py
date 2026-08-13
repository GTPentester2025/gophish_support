#!/usr/bin/env python3
"""Compatibility entrypoint. Delegates to main.py.

Prefer: python main.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import main

if __name__ == "__main__":
    raise SystemExit(main())
