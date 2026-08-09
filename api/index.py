import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "python-service"
sys.path.insert(0, str(SERVICE_ROOT))

from app.main import app

__all__ = ["app"]
