"""Small, atomic JSON state store for polling/restart safety."""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "initialized": False,
    "was_live": False,
    "active_started_at": None,
    "pending_started_at": None,
    "last_processed_vod_id": None,
}


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return dict(DEFAULT_STATE)
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("state root is not an object")
            return {**DEFAULT_STATE, **loaded}
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            print(f"WARNING: state file could not be read ({exc}); using a clean state.")
            return dict(DEFAULT_STATE)

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.save()
