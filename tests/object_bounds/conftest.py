"""Pytest bootstrap for the object-bounds regression suite.

Ensures the repository root (containing the ``cairosvg`` package and the
independent ``geometry`` reference) is importable regardless of the directory
pytest is launched from.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for path in (str(ROOT), str(ROOT / 'tests' / 'object_bounds')):
    if path not in sys.path:
        sys.path.insert(0, path)
