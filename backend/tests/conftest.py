"""Make `app` importable no matter which directory pytest is invoked from.

Without this, `pytest` works from backend/ but fails at collection from the
repo root, which is a confusing first experience.
"""
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
