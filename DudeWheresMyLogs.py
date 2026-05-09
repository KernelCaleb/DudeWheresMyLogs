#!/usr/bin/env python3
"""Convenience wrapper to run DudeWheresMyLogs without installing."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dwml.cli import main

if __name__ == "__main__":
    main()
