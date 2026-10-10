import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """每个测试使用独立的配置目录与数据目录，不碰本机真实配置。"""
    home = tmp_path / "config"
    monkeypatch.setenv("QQARCHIVE_CONFIG_DIR", str(home))
    return home
