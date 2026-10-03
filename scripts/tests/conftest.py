"""Makes `scripts/` importable so the tests run with a plain `python -m pytest`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
