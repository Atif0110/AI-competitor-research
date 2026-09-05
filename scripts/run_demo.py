"""Run research against the example config (demo mode, no API keys).

Usage: python scripts/run_demo.py   (equivalent to: python research.py)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    import runpy

    runpy.run_path(str(Path(__file__).resolve().parents[1] / "research.py"), run_name="__main__")
