"""Latency and protocol probes for Gnani Prisma/Timbre and Amazon Nova Pro (owner: Builder 2).

The probes import settings and helpers from the `mockboard` package. If the package is not
installed (`pip install -e .`), this puts `src/` on the import path so the probes still run
from a plain checkout with `python -m probes.<name>`.
"""

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("mockboard") is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
