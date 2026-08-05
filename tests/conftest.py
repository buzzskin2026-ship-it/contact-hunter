import os
from pathlib import Path

os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("DATABASE_URL", "sqlite:///./data/test.db")
os.environ.setdefault("PLAYWRIGHT_ENABLED", "false")

Path("data").mkdir(exist_ok=True)
