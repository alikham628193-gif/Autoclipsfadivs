"""Small, atomic JSON state store for polling/restart safety."""
from __future__ import annotations
import copy
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_STATE: dict[str, Any] = {
    "initialized": False,
    "watermark": None,         # only VODs created after this moment are processed
    "done_vod_ids": [],        # successfully published
    "failed_vod_ids": [],      # gave up (permanent error or too many attempts)
    "forced_vod_ids": [],      # VODs requested through RETRY_VOD_IDS (ignore watermark)
    "retry_consumed": [],      # RETRY_VOD_IDS values already applied once
    "jobs": {},                # vod_id -> {attempts, downloaded, rendered, kbps, retry_after_ts}
}


def _fresh_default() -> dict[str, Any]:
    return copy.deepcopy(DEFAULT_STATE)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return _fresh_default()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("state root is not an object")
            merged = _fresh_default()
            merged.update(loaded)
            return merged
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            print(f"WARNING: state file could not be read ({exc}); using a clean state.")
            return _fresh_default()

    def save(self) -> None:
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(self.data, ensure_ascii=False, indent=2)
        tmp.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **values: Any) -> None:
        self.data.update(values)
        self.save()
