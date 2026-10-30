#!/usr/bin/env python3
"""Legacy command entry point; all backend behavior lives in scenula.py."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("scenula.py")), run_name="__main__")
