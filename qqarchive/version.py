"""版本信息，统一从 version.json 读取。"""

from __future__ import annotations

import json
from pathlib import Path

from .resources import resource_path

_info = json.loads(Path(resource_path("version.json")).read_text(encoding="utf-8"))

APP_NAME: str = _info["app"]
VERSION: str = _info["version"]
BUILD: str = _info["build"]
SCHEMA_VERSION: int = int(_info["schema"])
RELEASE_DATE: str = _info["date"]
