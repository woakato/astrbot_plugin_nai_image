"""Pytest bootstrap: make the repository root importable.

Without this, running a bare ``pytest`` (instead of ``python -m pytest``)
does not put the repository root on ``sys.path``, so ``import main`` in the
test modules either fails or silently resolves to an unrelated ``main.py``
found elsewhere on ``sys.path``.
"""

import sys
from pathlib import Path

ROOT_DIR = str(Path(__file__).resolve().parent)

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
