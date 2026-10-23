import pytest

from app import db
from app.config import cfg


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # a fake token and an unresolvable host: a test that forgets to stub the network fails instead of hitting MAX
    monkeypatch.setattr(cfg, "MAX_BOT_TOKEN", "test-token")
    monkeypatch.setattr(cfg, "MAX_API_BASE", "https://max.invalid")
    monkeypatch.setattr(cfg, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(cfg, "DB_PATH", str(tmp_path / "korsik.db"))
    db.init_db()
