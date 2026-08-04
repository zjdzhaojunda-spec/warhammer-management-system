from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("WMS_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "win32":
        path = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WMS"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "WMS"
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "wms"
    path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    root = data_dir()
    database_dir = root / "database"
    database_dir.mkdir(parents=True, exist_ok=True)
    target = database_dir / "wms.sqlite3"
    legacy = root / "wms.sqlite3"
    if not target.exists() and legacy.exists():
        shutil.move(str(legacy), str(target))
        for suffix in ("-wal", "-shm"):
            legacy_sidecar = Path(f"{legacy}{suffix}")
            if legacy_sidecar.exists():
                shutil.move(str(legacy_sidecar), str(Path(f"{target}{suffix}")))
    return target


def rules_root() -> Path:
    return data_dir()
