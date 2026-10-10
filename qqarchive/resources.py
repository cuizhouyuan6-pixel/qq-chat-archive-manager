"""定位随程序分发的资源文件（源码运行与 PyInstaller 打包两种情况）。"""

from __future__ import annotations

import sys
from pathlib import Path


def package_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / "qqarchive"
    return Path(__file__).resolve().parent


def resource_path(*parts: str) -> Path:
    return package_dir().joinpath(*parts)
