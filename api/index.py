import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Vercel Python runtime expects a top-level WSGI callable named `app`.
from app import app

__all__ = ["app"]
