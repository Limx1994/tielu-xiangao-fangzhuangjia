from __future__ import annotations

import copy
import heapq
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable


class ManifestStore:
    INDEX_VERSION = 1

    def __init__(self, event_dir: Path, atomic_json: Callable[[Path, Any], None], logger: Any, utc_now: Callable[[], str]) -> None:
        self.event_dir, self.atomic_json = event_dir, atomic_json
        self.logger, self.utc_now = logger, utc_now
        self.index_path = event_dir / ".manifest-index.json"
        self.lock = threading.RLock(); self.loaded = False
        self.items: dict[Path, tuple[int, dict[str, Any]]] = {}
        self.recent: dict[Path, tuple[int, dict[str, Any]]] = {}

    @staticmethod
    def _is_active(manifest: dict[str, Any]) -> bool:
        if manifest.get("recording", {}).get("status") in ("waiting", "pending"):
            return True
        if manifest.get("ocr", {}).get("status") == "pending":
            return True
        if any(manifest.get(key, {}).get("status") in ("pending", "retry") for key in ("text_upload", "video_upload")):
            return True
        upload = manifest.get("config", {}).get("upload", {})
        uploads_complete = all(manifest.get(key, {}).get("status") == "complete" for key in ("text_upload", "video_upload"))
        return bool(upload.get("delete_after_success") and uploads_complete and manifest.get("video_path") and Path(manifest["video_path"]).exists())

    def _remember_recent(self, path: Path, modified: int, manifest: dict[str, Any]) -> None:
        self.recent[path] = (modified, copy.deepcopy(manifest))
        if len(self.recent) > 200:
            oldest = min(self.recent, key=lambda item: self.recent[item][0])
            self.recent.pop(oldest)

    def _relative_path(self, path: Path) -> str:
        return str(path.relative_to(self.event_dir))

    def _indexed_path(self, value: Any) -> Path:
        if not isinstance(value, str):
            raise ValueError("索引路径不是字符串")
        relative = Path(value)
        if relative.is_absolute() or len(relative.parts) != 3 or relative.parts[-1] != "manifest.json" or ".." in relative.parts:
            raise ValueError(f"索引路径无效: {value}")
        return self.event_dir / relative

    def _index_data(self) -> dict[str, Any]:
        active = sorted(self._relative_path(path) for path in self.items)
        recent = [
            {"path": self._relative_path(path), "mtime_ns": modified}
            for path, (modified, _) in sorted(self.recent.items(), key=lambda item: item[1][0], reverse=True)
        ]
        return {"version": self.INDEX_VERSION, "active": active, "recent": recent}

    def _write_index(self) -> None:
        self.atomic_json(self.index_path, self._index_data())

    def _rebuild_index(self, reason: str) -> None:
        recent: list[tuple[int, str, Path, dict[str, Any]]] = []
        scanned = 0
        self.items.clear(); self.recent.clear()
        for path in self.event_dir.glob("*/*/manifest.json"):
            scanned += 1
            try:
                modified, manifest = path.stat().st_mtime_ns, self.load(path)
                if self._is_active(manifest): self.items[path] = (modified, manifest)
                entry = (modified, str(path), path, manifest)
                if len(recent) < 200: heapq.heappush(recent, entry)
                elif entry[:2] > recent[0][:2]: heapq.heapreplace(recent, entry)
            except Exception as exc: self.logger.error("清单读取失败 %s: %s", path, exc)
        self.recent = {path: (modified, manifest) for modified, _, path, manifest in recent}
        self.logger.warning("清单索引全量重建：原因=%s，扫描=%s，active=%s，recent=%s", reason, scanned, len(self.items), len(self.recent))
        self._write_index()

    def _load_index(self) -> None:
        with self.lock:
            if self.loaded: return
            try:
                if not self.index_path.exists(): raise FileNotFoundError("索引不存在（首次迁移或被删除）")
                with self.index_path.open("r", encoding="utf-8") as stream: index = json.load(stream)
                if not isinstance(index, dict) or index.get("version") != self.INDEX_VERSION: raise ValueError("索引版本无效")
                active_values, recent_values = index.get("active"), index.get("recent")
                if not isinstance(active_values, list) or not isinstance(recent_values, list) or len(recent_values) > 200: raise ValueError("索引结构无效")
                active_paths = [self._indexed_path(value) for value in active_values]
                recent_paths = [self._indexed_path(value.get("path")) for value in recent_values if isinstance(value, dict) and isinstance(value.get("mtime_ns"), int)]
                active_set, recent_set = set(active_paths), set(recent_paths)
                if len(recent_paths) != len(recent_values) or len(active_set) != len(active_paths) or len(recent_set) != len(recent_paths): raise ValueError("索引包含无效或重复条目")
                self.items.clear(); self.recent.clear()
                for path in dict.fromkeys(active_paths + recent_paths):
                    try:
                        modified, manifest = path.stat().st_mtime_ns, self.load(path)
                    except Exception as exc:
                        self.logger.error("索引清单读取失败 %s: %s", path, exc)
                        continue
                    is_active = self._is_active(manifest)
                    if path in active_set and is_active: self.items[path] = (modified, manifest)
                    if path in active_set or path in recent_set: self._remember_recent(path, modified, manifest)
                if self._index_data() != index: self._write_index()
                self.logger.info("清单索引加载完成：读取=%s，active=%s，recent=%s", len(active_set | recent_set), len(self.items), len(self.recent))
            except Exception as exc:
                self._rebuild_index(f"{type(exc).__name__}: {exc}")
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
        path = self.path(manifest["event_id"], manifest["camera_id"])
        self._load_index()
        with self.lock:
            saved, active = copy.deepcopy(manifest), self._is_active(manifest)
            pre_registered = False
            if active and path not in self.items:
                self.items[path] = (time.time_ns(), saved)
                try: self._write_index()
                except Exception:
                    self.items.pop(path, None)
                    raise
                pre_registered = True
            try: self.atomic_json(path, manifest)
            except Exception:
                if pre_registered: self.items.pop(path, None)
                raise
            modified = path.stat().st_mtime_ns
            if active: self.items[path] = (modified, saved)
            else: self.items.pop(path, None)
            self._remember_recent(path, modified, saved)
            self._write_index()

    def iter_all(self, limit: int = 0) -> Iterable[dict[str, Any]]:
        self._load_index()
        if limit > 200:
            values = []
            for path in self.event_dir.glob("*/*/manifest.json"):
                try: values.append((path.stat().st_mtime_ns, self.load(path)))
                except Exception as exc: self.logger.error("清单读取失败 %s: %s", path, exc)
            values.sort(key=lambda item: item[0], reverse=True)
            return [copy.deepcopy(item[1]) for item in values[:limit]]
        with self.lock:
            source = self.recent if limit else self.items
            values = sorted(source.values(), key=lambda item: item[0], reverse=True)
        return [copy.deepcopy(item[1]) for item in values[:limit or None]]
