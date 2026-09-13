from __future__ import annotations

import copy
import json
import threading
from pathlib import Path
from typing import Any, Callable, Iterable


class ManifestStore:
    def __init__(self, event_dir: Path, atomic_json: Callable[[Path, Any], None], logger: Any, utc_now: Callable[[], str]) -> None:
        self.event_dir, self.atomic_json = event_dir, atomic_json
        self.logger, self.utc_now = logger, utc_now
        self.lock = threading.RLock(); self.loaded = False
        self.items: dict[Path, tuple[int, dict[str, Any]]] = {}

    def _load_index(self) -> None:
        with self.lock:
            if self.loaded: return
            for path in self.event_dir.glob("*/*/manifest.json"):
                try: self.items[path] = (path.stat().st_mtime_ns, self.load(path))
                except Exception as exc: self.logger.error("清单读取失败 %s: %s", path, exc)
            self.loaded = True

    def path(self, event_id: str, camera_id: str) -> Path:
        return self.event_dir / event_id / camera_id / "manifest.json"

    def create(self, event_id: str, camera: dict[str, Any], trigger_time: float, config: dict[str, Any]) -> dict[str, Any]:
        directory = self.path(event_id, camera["id"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "event_id": event_id, "camera_id": camera["id"], "trigger_time": trigger_time,
            "created_at": self.utc_now(), "config": config, "video_path": str(directory / "event.mp4"),
            "coverage": {"requested_start": trigger_time - config["recording"]["pre_seconds"], "requested_end": trigger_time + config["recording"]["post_seconds"], "actual_start": None, "actual_end": None, "complete": False, "reason": "等待后录"},
            "recording": {"status": "waiting", "error": None},
            "ocr": {"status": "pending", "plates": [], "frames": 0, "failed_frames": 0, "error": None},
            "text_upload": {"status": "pending", "attempts": 0, "next_retry": 0, "error": None, "receipt": None},
            "video_upload": {"status": "pending", "attempts": 0, "next_retry": 0, "error": None, "receipt": None},
        }
        self.save(manifest)
        return manifest

    def load(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream: return json.load(stream)

    def save(self, manifest: dict[str, Any]) -> None:
        path = self.path(manifest["event_id"], manifest["camera_id"]); self.atomic_json(path, manifest)
        with self.lock:
            if self.loaded: self.items[path] = (path.stat().st_mtime_ns, copy.deepcopy(manifest))

    def iter_all(self, limit: int = 0) -> Iterable[dict[str, Any]]:
        self._load_index()
        with self.lock: values = sorted(self.items.values(), key=lambda item: item[0], reverse=True)
        return [copy.deepcopy(item[1]) for item in values[:limit or None]]
