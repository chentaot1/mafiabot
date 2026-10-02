from __future__ import annotations

import sys
from pathlib import Path


# Ensure repository root is importable (so `import game`, `import database`, etc. work).
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
