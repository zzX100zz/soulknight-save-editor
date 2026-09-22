#!/usr/bin/env python3
"""Entry point - `python3 run.py` for the menu, `python3 run.py --help` for usage."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sksave.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
