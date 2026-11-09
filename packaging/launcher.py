"""Frozen-app entry point (PyInstaller)."""

import multiprocessing
import sys

from huddle_helper.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
