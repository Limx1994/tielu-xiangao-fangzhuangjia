from __future__ import annotations
import copy
from collections import deque
import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlparse
import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from urllib3.util import Timeout
from flask import Flask, Response, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - dependency health reports this
    serial = list_ports = None
APP_NAME = "XgfzjRecorder"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", BASE_DIR))
RUNTIME_DIR = BASE_DIR / "runtime"
CACHE_DIR = RUNTIME_DIR / "cache"
EVENT_DIR = RUNTIME_DIR / "events"
LOG_DIR = RUNTIME_DIR / "logs"
for directory in (RUNTIME_DIR, CACHE_DIR, EVENT_DIR, LOG_DIR):
    directory.mkdir(parents=True, exist_ok=True)
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
STARTUPINFO = None
if os.name == "nt":
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    "http": {"host": "127.0.0.1", "port": 5000},
    "recording": {"pre_seconds": 120, "post_seconds": 20, "segment_seconds": 10},
    "preview": {"width": 640, "fps": 5, "idle_seconds": 15},
    "ocr": {"enabled": True, "fps": 1, "confidence": 0.8, "port": 8081, "cpu_threads": 2},
    "upload": {
        "delete_after_success": False,
        "connect_timeout": 5,
        "read_timeout": 30,
        "total_timeout": 1800,
        "max_retries": 8,
        "text_url": "",
        "video_url": "",
        "token": "",
    },
    "storage": {"reserve_percent": 5, "reserve_bytes": 2147483648, "cache_keep_seconds": 180},
    "cameras": [],
    "serial_ports": [{"device_id": "trigger_1", "baudrate": 9600, "mode": "text", "trigger": "XGFZJ:TRIGGER:trigger_1:1\r\n", "enabled": True}],
}
SERIAL_PROBE = b"XGFZJ:DISCOVER:1\r\n"
class AppError(Exception):
    def __init__(self, message: str, code: str = "APP_ERROR", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None),
        }
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)
logger = logging.getLogger(APP_NAME)
logger.setLevel(logging.INFO)
handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
handler.setFormatter(JsonFormatter())
logger.addHandler(handler)
def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
def deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result
def process_args(args: list[str]) -> dict[str, Any]:
    return {"startupinfo": STARTUPINFO, "creationflags": CREATE_NO_WINDOW}
def drain_pipe(pipe, sink: deque, sentinel) -> None:
    if pipe:
        sink.extend(iter(pipe.readline, sentinel))
def pipe_chunks(source, output: queue.Queue[bytes]) -> None:
    while source and (chunk := source.read(8192)):
        try: output.put(chunk, timeout=1)
        except queue.Full: return
def stop_process(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None: return
    process.terminate()
    try: process.wait(timeout=5)
    except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=2)
def find_binary(name: str, bundled: str) -> Path:
    local = BASE_DIR / bundled
    if local.exists():
        return local
    found = shutil.which(name)
    return Path(found) if found else local
FFMPEG = find_binary("ffmpeg", "tools/ffmpeg/bin/ffmpeg.exe")
FFPROBE = find_binary("ffprobe", "tools/ffmpeg/bin/ffprobe.exe")
OCR_DIR = BASE_DIR / "tools" / "ocr"
def validate_config(config: dict[str, Any]) -> None:
    try:
        http = config["http"]
        if http["host"] not in ("127.0.0.1", "localhost") or not 1 <= int(http["port"]) <= 65535: raise ValueError("网页监听地址或端口无效")
        rec = config["recording"]
        if not 0 <= int(rec["pre_seconds"]) <= 3600 or not 0 <= int(rec["post_seconds"]) <= 600: raise ValueError("录像时长超出范围")
        if not 2 <= int(rec["segment_seconds"]) <= 60: raise ValueError("切片时长必须为 2-60 秒")
        preview = config["preview"]
        if not 160 <= int(preview["width"]) <= 1920 or not 1 <= int(preview["fps"]) <= 15:
            raise ValueError("预览参数超出范围")
        if not 5 <= int(preview["idle_seconds"]) <= 600: raise ValueError("预览空闲时间必须为 5-600 秒")
        ocr = config["ocr"]
        if not 0 <= float(ocr["confidence"]) <= 1: raise ValueError("OCR 置信度必须在 0-1")
        if not 1 <= int(ocr["fps"]) <= 10 or not 1 <= int(ocr["cpu_threads"]) <= 16: raise ValueError("OCR 帧率或线程数超出范围")
        upload = config["upload"]
        if not 1 <= float(upload["connect_timeout"]) <= 300 or not 1 <= float(upload["read_timeout"]) <= 600 or not 10 <= float(upload["total_timeout"]) <= 86400 or not 1 <= int(upload["max_retries"]) <= 100: raise ValueError("上传超时或重试参数超出范围")
        for key in ("text_url", "video_url"):
            parsed = urlparse(str(upload[key]));
            if upload[key] and (parsed.scheme not in ("http", "https") or not parsed.hostname): raise ValueError("上传 URL 无效")
        storage = config["storage"]
        if not 0 <= float(storage["reserve_percent"]) <= 50 or not 0 <= int(storage["reserve_bytes"]) or not 30 <= int(storage["cache_keep_seconds"]) <= 86400: raise ValueError("磁盘保留参数超出范围")
        if not isinstance(config.get("cameras"), list) or not isinstance(config.get("serial_ports"), list) or any(not isinstance(item, dict) for item in config["cameras"] + config["serial_ports"]): raise ValueError("设备配置必须为数组对象")
        if len(config.get("cameras", [])) > 2 or len(config.get("serial_ports", [])) > 16:
            raise ValueError("设备数量超出限制")
        ids = [str(item.get("id", "")) for item in config.get("cameras", [])]
        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", item) for item in ids) or len(ids) != len(set(ids)):
            raise ValueError("摄像头 ID 无效或重复")
        for camera in config.get("cameras", []):
            url = urlparse(camera_url(camera, "rtsp_url"))
            if url.scheme.lower() != "rtsp" or not url.hostname:
                raise ValueError(f"摄像头 {camera.get('id')} 的 RTSP 地址无效")
        serial_ids = []
        for port in config.get("serial_ports", []):
            device_id = str(port.get("device_id", "")); serial_ids.append(device_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", device_id): raise ValueError("串口设备 ID 无效")
            if port.get("mode") not in ("hex", "text"): raise ValueError("串口触发模式必须为 hex 或 text")
            if not 300 <= int(port.get("baudrate", 9600)) <= 4_000_000: raise ValueError("串口波特率无效")
            parse_trigger(port)
        if len(serial_ids) != len(set(serial_ids)): raise ValueError("串口设备 ID 重复")
    except (KeyError, TypeError, ValueError) as exc:
        raise AppError(str(exc), "INVALID_CONFIG", 422) from exc
def parse_trigger(item: dict[str, Any]) -> bytes:
    value = str(item.get("trigger", ""))
    if not value:
        raise ValueError("触发指令不能为空")
    if item.get("mode") == "hex":
        compact = re.sub(r"[\s:-]", "", value)
        if len(compact) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", compact):
            raise ValueError("HEX 触发指令无效")
        return bytes.fromhex(compact)
    return value.encode("utf-8")
def serial_identity(device_id: str) -> bytes:
    return f"XGFZJ:DEVICE:{device_id}:1\r\n".encode("ascii")
def probe_serial(stream: Any, device_id: str, timeout: float = 0.8) -> bool:
    expected, received, deadline = serial_identity(device_id), bytearray(), time.monotonic() + timeout
    stream.reset_input_buffer(); stream.write(SERIAL_PROBE); stream.flush()
    while time.monotonic() < deadline and len(received) < 4096:
        chunk = stream.read(max(1, min(int(getattr(stream, "in_waiting", 0)), 256)))
        if chunk: received.extend(chunk)
        if expected in received: return True
    return False
def camera_url(camera: dict[str, Any], key: str) -> str:
    value = str(camera.get(key, ""))
    if not value and key == "preview_url":
        return camera_url(camera, "rtsp_url")
    return value
def authenticated_url(value: str, username: str, password: str) -> str:
    if not username: return value
    parsed = urlparse(value); host = f"[{parsed.hostname}]" if ":" in (parsed.hostname or "") else parsed.hostname
    netloc = f"{quote(username, safe='')}:{quote(password, safe='')}@{host}{f':{parsed.port}' if parsed.port else ''}"
    return parsed._replace(netloc=netloc).geturl()
def scrub_message(value: str) -> str:
    return re.sub(r"(rtsp://)[^/@\s]+@", r"\1***@", value)
def hide_secret(value: str, secret: str) -> str: return value.replace(secret, "***") if secret else value
def probe_rtsp(value: str, username: str, password: str) -> dict[str, Any]:
    command = [str(FFPROBE), "-v", "error", "-rtsp_transport", "tcp", "-timeout", "5000000", "-select_streams", "v:0", "-show_entries", "stream=codec_name,width,height", "-of", "json", authenticated_url(value, username, password)]
    try: result = subprocess.run(command, capture_output=True, text=True, timeout=8, **process_args([]))
    except Exception as exc: return {"validated": False, "codec": None, "error": scrub_message(hide_secret(str(exc), password))[:160]}
    if result.returncode: return {"validated": False, "codec": None, "error": scrub_message(hide_secret(result.stderr, password))[-160:]}
    streams = json.loads(result.stdout).get("streams", [])
    stream = streams[0] if streams else {}
    return {"validated": bool(streams), "codec": stream.get("codec_name"), "width": stream.get("width"), "height": stream.get("height"), "error": None if streams else "未发现视频流"}
class DeadlineBody:
    def __init__(self, encoder: MultipartEncoder, seconds: float) -> None:
        self.encoder, self.len, self.deadline = encoder, encoder.len, time.monotonic() + seconds
    @property
    def content_type(self) -> str: return self.encoder.content_type
    def read(self, size: int = -1) -> bytes:
        if time.monotonic() > self.deadline: raise requests.Timeout("上传超过总时限")
        return self.encoder.read(size)
class ConfigStore:
    def __init__(self) -> None:
        self.path = BASE_DIR / "config.json"
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULT_CONFIG)
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as stream:
                self.data = deep_merge(self.data, json.load(stream))
        validate_config(self.data)
        if not self.path.exists():
            atomic_json(self.path, self.data)
    def get(self) -> dict[str, Any]:
        with self.lock:
            return copy.deepcopy(self.data)
    def save(self, incoming: dict[str, Any], secrets_update: dict[str, str] | None = None) -> dict[str, Any]:
        current = self.get()
        candidate = deep_merge(DEFAULT_CONFIG, incoming)
        old_cameras = {item["id"]: item for item in current["cameras"]}
        for camera in candidate["cameras"]:
            old = old_cameras.get(str(camera.get("id", "")), {})
            for key in ("rtsp_url", "preview_url"):
                if re.match(r"rtsp://\*\*\*@", str(camera.get(key, "")), re.I): camera[key] = old.get(key, "")
        for key in ("text_url", "video_url", "token"):
            value = (secrets_update or {}).get(key)
            candidate["upload"][key] = value if value else current["upload"].get(key, "")
        validate_config(candidate)
        with self.lock:
            old = self.data
            try:
                atomic_json(self.path, candidate)
                self.data = candidate
                logger.info("配置已保存")
            except Exception:
                self.data = old
                raise
        return self.get()
    def public(self) -> dict[str, Any]:
        data = self.get()
        upload = data["upload"]
        data["secret_status"] = {
            "text_url": bool(upload["text_url"]),
            "video_url": bool(upload["video_url"]),
            "token": bool(upload["token"]),
        }
        for key in ("text_url", "video_url", "token"): upload[key] = ""
        for camera in data["cameras"]:
            for key in ("rtsp_url", "preview_url"): camera[key] = scrub_message(str(camera.get(key, "")))
        return data
class ManifestStore:
    def path(self, event_id: str, camera_id: str) -> Path:
        return EVENT_DIR / event_id / camera_id / "manifest.json"
    def create(self, event_id: str, camera: dict[str, Any], trigger_time: float, config: dict[str, Any]) -> dict[str, Any]:
        directory = self.path(event_id, camera["id"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {
            "event_id": event_id, "camera_id": camera["id"], "trigger_time": trigger_time,
            "created_at": utc_now(), "config": config, "video_path": str(directory / "event.mp4"),
            "coverage": {"requested_start": trigger_time - config["recording"]["pre_seconds"], "requested_end": trigger_time + config["recording"]["post_seconds"], "actual_start": None, "actual_end": None, "complete": False, "reason": "等待后录"},
            "recording": {"status": "waiting", "error": None},
            "ocr": {"status": "pending", "plates": [], "frames": 0, "failed_frames": 0, "error": None},
            "text_upload": {"status": "pending", "attempts": 0, "next_retry": 0, "error": None, "receipt": None},
            "video_upload": {"status": "pending", "attempts": 0, "next_retry": 0, "error": None, "receipt": None},
        }
        atomic_json(self.path(event_id, camera["id"]), manifest)
        return manifest
    def load(self, path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(stream)
    def save(self, manifest: dict[str, Any]) -> None:
        atomic_json(self.path(manifest["event_id"], manifest["camera_id"]), manifest)
    def iter_all(self, limit: int = 0) -> Iterable[dict[str, Any]]:
        paths = sorted(EVENT_DIR.glob("*/*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in paths[:limit or None]:
            try:
                yield self.load(path)
            except Exception as exc:
                logger.error("清单读取失败 %s: %s", path, exc)
def probe_duration(path: Path) -> float:
    result = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, timeout=20, **process_args([]))
    if result.returncode:
        raise AppError(result.stderr.strip() or "ffprobe 失败", "PROBE_FAILED", 500)
    return float(result.stdout.strip())
def stream_signature(path: Path) -> tuple[Any, ...]:
    result = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name,profile,width,height,pix_fmt,level", "-of", "json", str(path)], capture_output=True, text=True, timeout=20, **process_args([]))
    if result.returncode: raise AppError(result.stderr.strip() or "ffprobe 失败", "PROBE_FAILED", 500)
    streams = json.loads(result.stdout).get("streams", [])
    if not streams: raise AppError("切片没有视频流", "NO_VIDEO_STREAM", 500)
    stream = streams[0]
    return tuple(stream.get(key) for key in ("codec_name", "profile", "width", "height", "pix_fmt", "level"))
class Recorder:
    def __init__(self, camera: dict[str, Any], config: dict[str, Any], stop: threading.Event) -> None:
        self.camera, self.config, self.stop = camera, config, stop
        self.closed = threading.Event()
        self.directory = CACHE_DIR / camera["id"]
        self.directory.mkdir(parents=True, exist_ok=True)
        self.process: subprocess.Popen | None = None
        self.status = {"state": "starting", "error": None, "restarts": 0}
        self.thread = threading.Thread(target=self._supervise, name=f"rec-{camera['id']}", daemon=True)
    def start(self) -> None:
        self.thread.start()
    def closed_segments(self) -> list[tuple[Path, float, float]]:
        files = sorted(self.directory.glob("*.ts"), key=lambda p: p.stat().st_mtime)
        if files and self.process and self.process.poll() is None:
            files = files[:-1]
        result = []
        for path in files:
            try:
                end = path.stat().st_mtime
                result.append((path, end - probe_duration(path), end))
            except Exception as exc:
                logger.warning("切片探测失败 %s: %s", path, exc)
        return result
    def _command(self) -> list[str]:
        seconds = int(self.config["recording"]["segment_seconds"])
        pattern = str(self.directory / "%Y%m%d_%H%M%S.ts")
        return [str(FFMPEG), "-hide_banner", "-loglevel", "warning", "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", camera_url(self.camera, "rtsp_url"), "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "segment", "-segment_time", str(seconds), "-reset_timestamps", "1", "-strftime", "1", pattern]
    def _supervise(self) -> None:
        delay = 1
        while not self.stop.is_set() and not self.closed.is_set():
            try:
                self.process = subprocess.Popen(self._command(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, **process_args([]))
                errors: deque[str] = deque(maxlen=20); stderr_pipe = self.process.stderr
                drain = threading.Thread(target=drain_pipe, args=(stderr_pipe, errors, ""), daemon=True); drain.start()
                self.status.update(state="running", error=None)
                while self.process.poll() is None and not self.stop.is_set() and not self.closed.wait(1):
                    pass
                if self.stop.is_set() or self.closed.is_set():
                    stop_process(self.process)
                    break
                drain.join(1); stderr = "".join(errors)[-1000:]
                self.status.update(state="reconnecting", error=scrub_message(stderr.strip()) or f"FFmpeg 退出 {self.process.returncode}", restarts=self.status["restarts"] + 1)
                logger.warning("录像流 %s 重连 %s: %s", self.camera["id"], self.status["restarts"], self.status["error"])
            except Exception as exc:
                self.status.update(state="reconnecting", error=scrub_message(str(exc)), restarts=self.status["restarts"] + 1)
            if self.closed.wait(delay) or self.stop.is_set():
                break
            delay = min(delay * 2, 30)
        self.status["state"] = "stopped"
    def close(self) -> None:
        self.closed.set()
        stop_process(self.process)
class Preview:
    def __init__(self, camera: dict[str, Any], config: dict[str, Any]) -> None:
        self.camera, self.config = camera, config
        self.closed = threading.Event()
        self.lock = threading.Condition()
        self.frame: bytes | None = None
        self.viewers = 0
        self.last_viewer = 0.0
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.mode = "未启动"
    def subscribe(self) -> Iterable[bytes]:
        with self.lock:
            self.viewers += 1
            self.last_viewer = time.time()
            if not self.thread or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name=f"preview-{self.camera['id']}", daemon=True)
                self.thread.start()
        try:
            last = None
            while True:
                with self.lock:
                    self.lock.wait_for(lambda last=last: self.frame is not None and self.frame is not last, timeout=5)
                    frame = self.frame
                    self.last_viewer = time.time()
                if frame:
                    last = frame
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        finally:
            with self.lock:
                self.viewers = max(0, self.viewers - 1)
                self.last_viewer = time.time()
    def _commands(self) -> Iterable[tuple[str, list[str]]]:
        url = camera_url(self.camera, "preview_url")
        common = ["-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp", "-timeout", "15000000", "-fflags", "nobuffer"]
        out = ["-i", url, "-an", "-vf", f"fps={int(self.config['preview']['fps'])},scale={int(self.config['preview']['width'])}:-2", "-q:v", "6", "-threads", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
        for mode, accel in (("D3D11VA", ["-hwaccel", "d3d11va"]), ("DXVA2", ["-hwaccel", "dxva2"]), ("软件解码", [])):
            yield mode, [str(FFMPEG)] + common + accel + out
    def _run(self) -> None:
        for mode, command in self._commands():
            if self.viewers <= 0 or self.closed.is_set():
                return
            try:
                self.mode = mode
                self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_args([]))
                chunks: queue.Queue[bytes] = queue.Queue(maxsize=32); process = self.process; errors: deque[bytes] = deque(maxlen=20)
                threading.Thread(target=pipe_chunks, args=(process.stdout, chunks), daemon=True).start()
                threading.Thread(target=drain_pipe, args=(process.stderr, errors, b""), daemon=True).start()
                buffer, last_frame = bytearray(), time.monotonic()
                while process.poll() is None and not self.closed.is_set():
                    if self.viewers == 0 and time.time() - self.last_viewer > int(self.config["preview"]["idle_seconds"]):
                        process.terminate()
                        return
                    try: chunk = chunks.get(timeout=1)
                    except queue.Empty:
                        if time.monotonic() - last_frame > 12: raise AppError("预览连续 12 秒无新画面", "PREVIEW_STALLED", 502) from None
                        continue
                    buffer.extend(chunk)
                    while True:
                        start, end = buffer.find(b"\xff\xd8"), buffer.find(b"\xff\xd9")
                        if start < 0 or end < start:
                            break
                        frame = bytes(buffer[start:end + 2]); last_frame = time.monotonic()
                        del buffer[:end + 2]
                        with self.lock:
                            self.frame = frame
                            self.lock.notify_all()
                if process.returncode == 0: return
                raise AppError(scrub_message(b"".join(errors).decode("utf-8", "replace")[-1000:]) or f"FFmpeg 退出 {process.returncode}", "PREVIEW_FAILED", 502)
            except Exception as exc:
                logger.warning("预览 %s %s 失败: %s", self.camera["id"], mode, exc)
            finally:
                stop_process(self.process)
        self.mode = "预览失败"
        if self.viewers > 0 and not self.closed.wait(3):
            self.thread = threading.Thread(target=self._run, name=f"preview-{self.camera['id']}", daemon=True); self.thread.start()
    def close(self) -> None:
        self.closed.set()
        stop_process(self.process)
class OCRClient:
    plate_pattern = re.compile(r"^[\u4e00-\u9fff][A-Z][A-Z0-9]{5,6}$")
    def __init__(self, config: dict[str, Any], stop: threading.Event) -> None:
        self.config, self.stop = config, stop
        self.process: subprocess.Popen | None = None
        self.lock = threading.RLock()
        self.log_stream = None
    def ensure_service(self) -> None:
        if not self.config["ocr"]["enabled"]:
            raise AppError("OCR 已禁用", "OCR_DISABLED", 409)
        exe = OCR_DIR / "ppocr_worker.exe"
        model = OCR_DIR / "models" / "plate_rtdetr.onnx"
        if not exe.exists() or exe.stat().st_size < 100_000 or not model.exists() or model.stat().st_size < 1_000_000:
            raise AppError("OCR 成品或模型不存在/仍是 Git LFS 指针", "OCR_MISSING", 503)
        if self.process and self.process.poll() is None:
            return
        if self.process or self.log_stream: self.close()
        args = [str(exe), "--model_dir", str(OCR_DIR / "models"), "--cpu_threads", str(self.config["ocr"]["cpu_threads"]), "--use_doc_orientation", "false"]
        ocr_log = LOG_DIR / "ocr.log"
        if ocr_log.exists() and ocr_log.stat().st_size >= 5_000_000: os.replace(ocr_log, LOG_DIR / "ocr.log.1")
        self.log_stream = ocr_log.open("a", encoding="utf-8")
        self.process = subprocess.Popen(args, cwd=OCR_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log_stream, text=True, encoding="utf-8", errors="replace", bufsize=1, **process_args([]))
        ready = read_line_timeout(self.process, 30)
        if ready.strip() != "READY":
            self.close()
            raise AppError(f"OCR worker 启动失败: {ready.strip() or '无响应'}", "OCR_START_FAILED", 503)
    def recognize(self, jpeg: bytes, config: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.lock:
            if config is not None: self.config = config
            self.ensure_service()
            frame_path = RUNTIME_DIR / f"ocr-{uuid.uuid4().hex}.jpg"
            try:
                frame_path.write_bytes(jpeg)
                if not self.process or not self.process.stdin:
                    raise OSError("OCR worker stdin 不可用")
                self.process.stdin.write(str(frame_path) + "\n")
                self.process.stdin.flush()
                line = read_line_timeout(self.process, 30)
                if not line.startswith("OK "):
                    raise AppError(line[4:].strip() if line.startswith("ERR ") else "OCR worker 响应无效", "OCR_FAILED", 502)
                payload = json.loads(line[3:]); merged: dict[str, list[Any]] = {"rec_texts": [], "rec_scores": []}
                for result in payload.get("results", []):
                    nested = result.get("result"); texts = re.search(r'"rec_texts"\s*:\s*(\[[^\]]*\])', nested) if isinstance(nested, str) else None; scores = re.search(r'"rec_scores"\s*:\s*(\[[^\]]*\])', nested) if isinstance(nested, str) else None
                    result = {"rec_texts": json.loads(texts.group(1)), "rec_scores": json.loads(scores.group(1))} if texts and scores else result
                    merged["rec_texts"].extend(result.get("rec_texts", [])); merged["rec_scores"].extend(result.get("rec_scores", []))
                return merged
            except (OSError, ValueError, json.JSONDecodeError, queue.Empty) as exc:
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                raise AppError(f"OCR 通信失败: {exc}", "OCR_IO", 502) from exc
            finally:
                frame_path.unlink(missing_ok=True)
    def close(self) -> None:
        with self.lock:
            stop_process(self.process)
            self.process = None
            if self.log_stream: self.log_stream.close(); self.log_stream = None
def read_line_timeout(process: subprocess.Popen, timeout: float) -> str:
    result: queue.Queue[str] = queue.Queue(maxsize=1)
    def read() -> None:
        result.put(process.stdout.readline() if process.stdout else "")
    threading.Thread(target=read, daemon=True).start()
    try: return result.get(timeout=timeout)
    except queue.Empty:
        if process.poll() is None: process.terminate()
        raise AppError("OCR worker 响应超时", "OCR_TIMEOUT", 504) from None
def jpeg_frames(video: Path, fps: int) -> Iterable[tuple[float, bytes]]:
    command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-i", str(video), "-vf", f"fps={fps}", "-q:v", "4", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **process_args([])); errors: deque[bytes] = deque(maxlen=20)
    drain = threading.Thread(target=drain_pipe, args=(process.stderr, errors, b""), daemon=True); drain.start()
    buffer, index = bytearray(), 0
    try:
        while True:
            chunk = process.stdout.read(65536) if process.stdout else b""
            if not chunk:
                break
            buffer.extend(chunk)
            if len(buffer) > 25_000_000:
                raise AppError("OCR 抽帧缓冲异常", "FRAME_TOO_LARGE", 500)
            while True:
                start, end = buffer.find(b"\xff\xd8"), buffer.find(b"\xff\xd9")
                if start < 0 or end < start:
                    break
                yield index / fps, bytes(buffer[start:end + 2])
                index += 1
                del buffer[:end + 2]
        code = process.wait(timeout=10)
        if code:
            drain.join(1); error = b"".join(errors).decode("utf-8", "replace")[-1000:]
            raise AppError(error or "FFmpeg 抽帧失败", "FRAME_EXTRACT_FAILED", 500)
    finally:
        stop_process(process)
def normalize_plate(text: str) -> str:
    return re.sub(r"[·•.\s_-]", "", text.upper())
class Runtime:
    def __init__(self) -> None:
        self.config_store = ConfigStore()
        self.config = self.config_store.get()
        self.manifests = ManifestStore()
        self.stop = threading.Event()
        self.recorders: dict[str, Recorder] = {}
        self.previews: dict[str, Preview] = {}
        self.serial_workers: list[threading.Thread] = []
        self.serial_generation = 0
        self.serial_lock = threading.Lock()
        self.serial_scan_lock = threading.Lock()
        self.serial_claims: dict[str, tuple[str, int]] = {}
        self.serial_status: dict[str, dict[str, Any]] = {}
        self.protected: dict[Path, int] = {}
        self.protected_lock = threading.Lock()
        self.queue_sets = {name: set() for name in ("stitch", "ocr", "upload")}
        self.queue_lock = threading.Lock()
        self.queues = {name: queue.Queue(maxsize=1000) for name in self.queue_sets}
        self.scan_lock = threading.Lock()
        self.scan_auth = ("", "")
        self.scan = {"status": "idle", "found": [], "completed": 0, "total": 0, "failures": {}, "cancel": False}
        self.status_errors: list[dict[str, Any]] = []
        self.accepting_events = True
        self.ocr_client = OCRClient(self.config, self.stop)
    def start(self) -> None:
        if not FFMPEG.exists() or not FFPROBE.exists():
            self.add_error("FFmpeg/ffprobe 不存在，录像功能不可用")
        usage = shutil.disk_usage(BASE_DIR)
        reserve = max(int(usage.total * float(self.config["storage"]["reserve_percent"]) / 100), int(self.config["storage"]["reserve_bytes"]))
        self._set_disk_state(usage.free >= reserve)
        self._start_recorders()
        self._start_serials()
        for stage, target in (("stitch", self._stitch_worker), ("ocr", self._ocr_worker), ("upload", self._upload_worker)):
            threading.Thread(target=target, name=f"worker-{stage}", daemon=True).start()
        threading.Thread(target=self._maintenance, name="maintenance", daemon=True).start()
        self._recover()
    def apply_config(self, config: dict[str, Any], secrets_update: dict[str, str] | None = None) -> dict[str, Any]:
        old = self.config
        saved = self.config_store.save(config, secrets_update)
        self.config = saved
        try:
            if old.get("cameras") != saved.get("cameras") or old.get("recording") != saved.get("recording") or old.get("preview") != saved.get("preview"):
                for item in self.recorders.values():
                    item.close()
                for item in self.previews.values():
                    item.close()
                self.recorders.clear(); self.previews.clear()
                self._start_recorders()
            if old.get("serial_ports") != saved.get("serial_ports"):
                self._start_serials()
            if old.get("ocr", {}).get("enabled") != saved["ocr"]["enabled"] or old.get("ocr", {}).get("cpu_threads") != saved["ocr"]["cpu_threads"]: self.ocr_client.close()
            self.ocr_client.config = saved
            return self.config_store.public()
        except Exception:
            self.config_store.save(old)
            self.config = old
            for item in self.recorders.values(): item.close()
            for item in self.previews.values(): item.close()
            self.recorders.clear(); self.previews.clear(); self._start_recorders(); self._start_serials(); self.ocr_client.config = old
            raise
    def _start_recorders(self) -> None:
        for camera in self.config.get("cameras", []):
            if camera.get("enabled", True):
                current = self.recorders.get(camera["id"])
                if self.accepting_events and (not current or current.closed.is_set()):
                    recorder = Recorder(camera, self.config, self.stop); self.recorders[camera["id"]] = recorder; recorder.start()
                if camera["id"] not in self.previews: self.previews[camera["id"]] = Preview(camera, self.config)
    def _start_serials(self) -> None:
        self.serial_generation += 1; generation = self.serial_generation
        with self.serial_lock: self.serial_status = {}
        for definition in self.config.get("serial_ports", []):
            if definition.get("enabled", True):
                device_id = definition["device_id"]
                with self.serial_lock: self.serial_status[device_id] = {"state": "scanning", "port": None, "checked": 0, "error": None}
                thread = threading.Thread(target=self._serial_loop, args=(copy.deepcopy(definition), generation), name=f"serial-{device_id}", daemon=True)
                self.serial_workers.append(thread); thread.start()
    def _serial_state(self, device_id: str, generation: int, **values: Any) -> None:
        with self.serial_lock:
            if generation == self.serial_generation and device_id in self.serial_status: self.serial_status[device_id].update(values)
    def _serial_loop(self, definition: dict[str, Any], generation: int) -> None:
        if serial is None or list_ports is None: self.add_error("PySerial 未安装"); return
        device_id, trigger = definition["device_id"], parse_trigger(definition)
        while not self.stop.is_set() and generation == self.serial_generation:
            try: ports = [item.device for item in list_ports.comports()]
            except Exception as exc: self.add_error(f"串口扫描失败: {exc}"); self.stop.wait(2); continue
            self._serial_state(device_id, generation, state="scanning", port=None, checked=len(ports))
            for port in ports:
                if self.stop.is_set() or generation != self.serial_generation: return
                with self.serial_scan_lock:
                    with self.serial_lock:
                        if port in self.serial_claims: continue
                    stream = None
                    try:
                        stream = serial.Serial(port, int(definition.get("baudrate", 9600)), timeout=0.15, write_timeout=0.5)
                        if not probe_serial(stream, device_id): stream.close(); continue
                        with self.serial_lock: self.serial_claims[port] = (device_id, generation)
                    except Exception:
                        try:
                            if stream: stream.close()
                        except Exception: pass
                        continue
                self._serial_state(device_id, generation, state="bound", port=port, error=None)
                logger.info("串口设备 %s 已绑定 %s", device_id, port)
                try: self._listen_serial(stream, definition, trigger, generation)
                except Exception as exc:
                    message = f"串口设备 {device_id} 在 {port} 断开: {exc}"; self.add_error(message); self._serial_state(device_id, generation, state="lost", port=None, error=str(exc)[:160])
                finally:
                    try:
                        if stream: stream.close()
                    except Exception: pass
                    with self.serial_lock:
                        if self.serial_claims.get(port) == (device_id, generation): self.serial_claims.pop(port, None)
                break
            else: self._serial_state(device_id, generation, error=f"尚未发现匹配设备，已检查 {len(ports)} 个串口")
            self.stop.wait(2)
    def _listen_serial(self, stream: Any, definition: dict[str, Any], trigger: bytes, generation: int) -> None:
        device_id = definition["device_id"]
        buffer, limit, last_probe, probe_deadline = bytearray(), max(4096, len(trigger) * 4), time.monotonic(), 0.0
        while not self.stop.is_set() and generation == self.serial_generation:
            chunk = stream.read(max(1, int(getattr(stream, "in_waiting", 0))))
            if chunk: buffer.extend(chunk)
            now, identity = time.monotonic(), serial_identity(device_id)
            if identity in buffer: buffer[:] = buffer.replace(identity, b"", 1); probe_deadline = 0.0
            if probe_deadline and now > probe_deadline: raise AppError("探测心跳无响应", "SERIAL_LOST")
            if now - last_probe >= 5:
                stream.write(SERIAL_PROBE); stream.flush(); last_probe, probe_deadline = now, now + 1
            while True:
                position = buffer.find(trigger)
                if position < 0:
                    if len(buffer) > limit: del buffer[:-max(1, len(trigger) - 1)]
                    break
                del buffer[:position + len(trigger)]
                try: self.trigger(f"serial:{device_id}")
                except AppError as exc: self.add_error(f"串口触发未接受: {exc.message}")
    def trigger(self, source: str) -> str:
        if not self.accepting_events:
            raise AppError("磁盘空间不足，暂停接收新事件", "DISK_LOW", 507)
        event_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        trigger_time, created = time.time(), 0
        for camera in self.config.get("cameras", []):
            if not camera.get("enabled", True):
                continue
            snapshot = copy.deepcopy(self.config)
            manifest = self.manifests.create(event_id, camera, trigger_time, snapshot)
            recorder = self.recorders.get(camera["id"])
            protected = [path for path in recorder.directory.glob("*.ts") if path.stat().st_mtime >= manifest["coverage"]["requested_start"]] if recorder else []
            self._protect(protected)
            manifest["protected_segments"] = [str(path) for path in protected]
            manifest["source"] = source
            self.manifests.save(manifest)
            self._enqueue("stitch", self.manifests.path(event_id, camera["id"]), delay=float(snapshot["recording"]["post_seconds"]) + float(snapshot["recording"]["segment_seconds"]) + 1)
            created += 1
        if not created:
            raise AppError("没有启用的摄像头", "NO_CAMERA", 409)
        logger.info("事件已接受 %s，摄像头 %s 路", event_id, created)
        return event_id
    def _protect(self, paths: Iterable[Path]) -> None:
        with self.protected_lock:
            for path in paths: self.protected[path] = self.protected.get(path, 0) + 1
    def _release(self, paths: Iterable[Path]) -> None:
        with self.protected_lock:
            for path in paths:
                count = self.protected.get(path, 0) - 1
                if count > 0: self.protected[path] = count
                else: self.protected.pop(path, None)
    def _enqueue(self, stage: str, path: Path, delay: float = 0) -> None:
        key = str(path)
        with self.queue_lock:
            if key in self.queue_sets[stage]: return
            self.queue_sets[stage].add(key)
        def put() -> None:
            try: self.queues[stage].put_nowait(path)
            except queue.Full:
                with self.queue_lock: self.queue_sets[stage].discard(key)
                self.add_error(f"{stage} 队列已满，任务暂缓: {path}")
        if delay:
            timer = threading.Timer(delay, put)
            timer.daemon = True
            timer.start()
        else:
            put()
    def _done_queue(self, stage: str, path: Path) -> None:
        with self.queue_lock: self.queue_sets[stage].discard(str(path))
        self.queues[stage].task_done()
    def _stitch_worker(self) -> None:
        while not self.stop.is_set():
            try: path = self.queues["stitch"].get(timeout=1)
            except queue.Empty: continue
            segments: list[tuple[Path, float, float]] = []; protected: list[Path] = []
            try:
                manifest = self.manifests.load(path)
                protected = [Path(value) for value in manifest.get("protected_segments", [])]
                camera_id, coverage = manifest["camera_id"], manifest["coverage"]
                recorder = self.recorders.get(camera_id)
                if not recorder:
                    raise AppError("摄像头录像器不存在", "RECORDER_MISSING", 500)
                segments = [item for item in recorder.closed_segments() if item[2] >= coverage["requested_start"] and item[1] <= coverage["requested_end"]]
                if not segments:
                    raise AppError("目标时间范围没有可用切片", "NO_SEGMENTS", 500)
                if len({stream_signature(item[0]) for item in segments}) != 1:
                    raise AppError("事件切片编码参数不一致，拒绝拼接", "INCOMPATIBLE_SEGMENTS", 409)
                extra = [item[0] for item in segments if item[0] not in protected]
                self._protect(extra); protected.extend(extra)
                manifest["protected_segments"] = [str(item) for item in protected]; self.manifests.save(manifest)
                list_path = path.parent / "concat.txt"
                list_path.write_text("".join(f"file '{str(item[0]).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for item in segments), encoding="utf-8")
                output = Path(manifest["video_path"])
                command = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(list_path), "-map", "0:v:0", "-an", "-c", "copy", "-movflags", "+faststart", "-y", str(output)]
                result = subprocess.run(command, capture_output=True, text=True, timeout=300, **process_args([]))
                if result.returncode or not output.exists() or output.stat().st_size == 0:
                    raise AppError(result.stderr[-1000:] or "视频拼接失败", "STITCH_FAILED", 500)
                probe_duration(output)
                coverage.update(actual_start=segments[0][1], actual_end=segments[-1][2])
                coverage["complete"] = coverage["actual_start"] <= coverage["requested_start"] and coverage["actual_end"] >= coverage["requested_end"]
                coverage["reason"] = None if coverage["complete"] else "启动缓存不足、断流或关键帧边界缺失"
                manifest["recording"].update(status="complete", error=None)
                manifest["protected_segments"] = []
                self.manifests.save(manifest)
                logger.info("事件 %s/%s 拼接完成，完整=%s", manifest["event_id"], camera_id, coverage["complete"])
                self._enqueue("ocr", path)
            except Exception as exc:
                self._fail_manifest(path, "recording", exc)
            finally:
                self._release(protected)
                self._done_queue("stitch", path)
    def _ocr_worker(self) -> None:
        while not self.stop.is_set():
            try: path = self.queues["ocr"].get(timeout=1)
            except queue.Empty: continue
            try:
                manifest = self.manifests.load(path)
                if manifest["recording"]["status"] != "complete":
                    continue
                config = manifest["config"]
                plates: dict[str, dict[str, Any]] = {}
                frames = failed = 0
                if config["ocr"]["enabled"]:
                    for timestamp, jpeg in jpeg_frames(Path(manifest["video_path"]), int(config["ocr"]["fps"])):
                        frames += 1
                        try:
                            data = self.ocr_client.recognize(jpeg, config)
                            texts, scores = data.get("rec_texts", []), data.get("rec_scores", [])
                            for text, score in zip(texts, scores, strict=False):
                                plate = normalize_plate(str(text))
                                score = float(score)
                                if score >= float(config["ocr"]["confidence"]) and OCRClient.plate_pattern.fullmatch(plate):
                                    current = plates.get(plate)
                                    if not current or score > current["confidence"]:
                                        plates[plate] = {"text": plate, "confidence": score, "timestamp": timestamp}
                        except Exception as exc:
                            failed += 1
                            logger.warning("OCR 帧失败 %s %.3f: %s", manifest["event_id"], timestamp, exc)
                status = "disabled" if not config["ocr"]["enabled"] else ("failed" if not frames or failed == frames else "partial" if failed else "complete" if plates else "no_plate")
                error = "未抽取到视频帧" if config["ocr"]["enabled"] and not frames else (f"{failed} 帧失败" if failed else None)
                manifest["ocr"].update(status=status, plates=list(plates.values()), frames=frames, failed_frames=failed, error=error)
                self.manifests.save(manifest)
                logger.info("事件 %s/%s OCR=%s，帧=%s，失败=%s", manifest["event_id"], manifest["camera_id"], status, frames, failed)
                self._enqueue("upload", path)
            except Exception as exc:
                self._fail_manifest(path, "ocr", exc)
                try: self._enqueue("upload", path)
                except Exception as queue_exc: self.add_error(f"上传入队失败: {queue_exc}")
            finally:
                self._done_queue("ocr", path)
    def _upload_worker(self) -> None:
        while not self.stop.is_set():
            try: path = self.queues["upload"].get(timeout=1)
            except queue.Empty: continue
            try: self._upload(path)
            except Exception as exc: self.add_error(f"上传任务异常 {path}: {exc}")
            finally: self._done_queue("upload", path)
    def _upload(self, path: Path) -> None:
        manifest = self.manifests.load(path)
        upload = manifest["config"]["upload"]
        video = Path(manifest["video_path"])
        if not video.exists():
            self._fail_manifest(path, "video_upload", AppError("视频文件不存在", "VIDEO_MISSING", 500), terminal=True)
            return
        payload = {key: manifest[key] for key in ("event_id", "camera_id", "trigger_time", "coverage", "ocr")}
        headers = {"Idempotency-Key": f"{manifest['event_id']}:{manifest['camera_id']}:text"}
        token = upload.get("token")
        if token: headers["Authorization"] = f"Bearer {token}"
        text_url, video_url = upload.get("text_url"), upload.get("video_url")
        if not text_url or not video_url:
            self._mark_waiting(manifest, "text_upload", "服务器地址未配置", terminal=True)
            self._mark_waiting(manifest, "video_upload", "服务器地址未配置", terminal=True)
            self.manifests.save(manifest); return
        session = requests.Session()
        timeout = Timeout(connect=float(upload["connect_timeout"]), read=float(upload["read_timeout"]), total=float(upload["total_timeout"]))
        if manifest["text_upload"]["status"] != "complete":
            try:
                response = session.post(text_url, json=payload, headers=headers, timeout=timeout)
                receipt = self._check_receipt(response, manifest, video=False)
                manifest["text_upload"].update(status="complete", error=None, receipt=receipt)
            except Exception as exc:
                self._retry_state(manifest, "text_upload", exc); self.manifests.save(manifest)
            self.manifests.save(manifest)
        if manifest["video_upload"]["status"] != "complete":
            digest = sha256_file(video)
            meta = json.dumps({**payload, "sha256": digest, "size": video.stat().st_size}, ensure_ascii=False)
            video_headers = {**headers, "Idempotency-Key": f"{manifest['event_id']}:{manifest['camera_id']}:video"}
            try:
                with video.open("rb") as stream:
                    body = DeadlineBody(MultipartEncoder(fields={"metadata": meta, "file": (video.name, stream, "video/mp4")}), float(upload["total_timeout"]))
                    video_headers["Content-Type"] = body.content_type
                    response = session.post(video_url, data=body, headers=video_headers, timeout=timeout)
                receipt = self._check_receipt(response, manifest, video=True, size=video.stat().st_size, digest=digest)
                manifest["video_upload"].update(status="complete", error=None, receipt=receipt)
            except Exception as exc:
                self._retry_state(manifest, "video_upload", exc); self.manifests.save(manifest)
            self.manifests.save(manifest)
        if upload.get("delete_after_success") and manifest["text_upload"]["status"] == manifest["video_upload"]["status"] == "complete":
            try: video.unlink(missing_ok=True)
            except OSError as exc:
                manifest.update(delete_error=str(exc), cleanup_next_retry=time.time() + 60); self.add_error(f"已上传视频清理失败 {video}: {exc}")
            else: manifest.update(local_deleted_at=utc_now(), delete_error=None, cleanup_next_retry=0)
            self.manifests.save(manifest)
        session.close()
    def _check_receipt(self, response: requests.Response, manifest: dict[str, Any], video: bool, size: int = 0, digest: str = "") -> dict[str, Any]:
        if not 200 <= response.status_code < 300:
            error = AppError(f"HTTP {response.status_code}: {scrub_message(response.text[:300])}", "UPLOAD_HTTP", response.status_code)
            error.retryable = response.status_code == 429 or response.status_code >= 500
            raise error
        try: receipt = response.json()
        except ValueError as exc: raise AppError("服务器回执不是 JSON", "INVALID_RECEIPT", 502) from exc
        valid = receipt.get("accepted") is True and receipt.get("event_id") == manifest["event_id"] and receipt.get("camera_id") == manifest["camera_id"]
        if video: valid = valid and int(receipt.get("size", -1)) == size and receipt.get("sha256") == digest
        if not valid: raise AppError("服务器回执字段不匹配", "INVALID_RECEIPT", 502)
        return receipt
    def _retry_state(self, manifest: dict[str, Any], key: str, exc: Exception) -> None:
        state, limit = manifest[key], int(manifest["config"]["upload"]["max_retries"])
        state["attempts"] += 1
        retryable = getattr(exc, "retryable", True)
        if not retryable or state["attempts"] >= limit:
            state.update(status="attention", next_retry=0, error=str(exc))
        else:
            delay = min(30 * (2 ** (state["attempts"] - 1)), 3600)
            state.update(status="retry", next_retry=time.time() + delay, error=str(exc))
        logger.warning("事件 %s/%s %s=%s，尝试=%s，原因=%s", manifest["event_id"], manifest["camera_id"], key, state["status"], state["attempts"], type(exc).__name__)
    def _mark_waiting(self, manifest: dict[str, Any], key: str, message: str, terminal: bool = False) -> None:
        manifest[key].update(status="attention" if terminal else "retry", next_retry=0 if terminal else time.time() + 30, error=message)
    def _fail_manifest(self, path: Path, key: str, exc: Exception, terminal: bool = True) -> None:
        logger.error("事件阶段失败 %s %s: %s", key, path, scrub_message(str(exc)))
        try:
            manifest = self.manifests.load(path)
            if key in ("recording", "ocr"):
                manifest[key].update(status="failed", error=str(exc))
                if key == "recording": manifest["protected_segments"] = []
            else:
                self._mark_waiting(manifest, key, str(exc), terminal)
            self.manifests.save(manifest)
        except Exception:
            logger.error("清单错误写入失败 %s\n%s", path, traceback.format_exc())
    def retry(self, event_id: str, camera_id: str) -> None:
        path = self.manifests.path(event_id, camera_id)
        if not path.exists(): raise AppError("事件不存在", "NOT_FOUND", 404)
        manifest = self.manifests.load(path)
        manifest["config"]["upload"] = copy.deepcopy(self.config["upload"])
        for key in ("text_upload", "video_upload"):
            if manifest[key]["status"] != "complete": manifest[key].update(status="pending", attempts=0, next_retry=0, error=None)
        self.manifests.save(manifest); self._enqueue("upload", path)
    def _recover(self) -> None:
        for manifest in self.manifests.iter_all():
            path = self.manifests.path(manifest["event_id"], manifest["camera_id"])
            if manifest["recording"]["status"] in ("waiting", "pending"):
                self._protect(Path(value) for value in manifest.get("protected_segments", []) if Path(value).exists())
                delay = max(0, float(manifest["coverage"]["requested_end"]) + float(manifest["config"]["recording"]["segment_seconds"]) + 1 - time.time())
                self._enqueue("stitch", path, delay)
            elif manifest["ocr"]["status"] == "pending": self._enqueue("ocr", path)
            elif manifest["video_upload"]["status"] in ("pending", "retry") or manifest["text_upload"]["status"] in ("pending", "retry"): self._enqueue("upload", path)
            elif manifest["config"]["upload"].get("delete_after_success") and Path(manifest["video_path"]).exists(): self._enqueue("upload", path)
    def _maintenance(self) -> None:
        while not self.stop.wait(10):
            try:
                usage = shutil.disk_usage(BASE_DIR)
                reserve = max(int(usage.total * float(self.config["storage"]["reserve_percent"]) / 100), int(self.config["storage"]["reserve_bytes"]))
                self._set_disk_state(usage.free >= reserve)
                cutoff = time.time() - max(int(self.config["storage"]["cache_keep_seconds"]), int(self.config["recording"]["pre_seconds"]) + int(self.config["recording"]["segment_seconds"]) * 2)
                for path in CACHE_DIR.glob("*/*.ts"):
                    with self.protected_lock:
                        if path not in self.protected and path.stat().st_mtime < cutoff:
                            try: path.unlink()
                            except OSError as exc: self.add_error(f"缓存清理失败 {path}: {exc}")
                now = time.time()
                for manifest in self.manifests.iter_all():
                    path = self.manifests.path(manifest["event_id"], manifest["camera_id"])
                    if manifest["recording"]["status"] in ("waiting", "pending") and manifest["coverage"]["requested_end"] < now:
                        self._enqueue("stitch", path)
                    if any(manifest[key]["status"] == "retry" and manifest[key]["next_retry"] <= now for key in ("text_upload", "video_upload")):
                        self._enqueue("upload", path)
                    if manifest["config"]["upload"].get("delete_after_success") and Path(manifest["video_path"]).exists() and manifest.get("cleanup_next_retry", 0) <= now and manifest["text_upload"]["status"] == manifest["video_upload"]["status"] == "complete": self._enqueue("upload", path)
            except Exception as exc: self.add_error(f"维护任务失败: {exc}")
    def _set_disk_state(self, enough: bool) -> None:
        if enough == self.accepting_events: return
        self.accepting_events = enough
        if enough: self._start_recorders(); logger.info("磁盘空间已恢复，录像缓存已重启")
        else:
            for item in self.recorders.values(): item.close()
            self.add_error("磁盘空间不足，已停止录像缓存并暂停新事件")
    def start_scan(self, subnet: str, port: int, username: str, password: str) -> None:
        try: network = ipaddress.ip_network(subnet, strict=False)
        except ValueError as exc: raise AppError("IPv4 网段无效", "INVALID_SUBNET", 422) from exc
        if network.version != 4 or network.num_addresses > 512:
            raise AppError("仅允许最多 512 个地址的 IPv4 网段", "INVALID_SUBNET", 422)
        with self.scan_lock:
            if self.scan["status"] == "running": raise AppError("扫描正在进行", "SCAN_RUNNING", 409)
            hosts = [str(ip) for ip in network.hosts()]
            scan_id = uuid.uuid4().hex
            self.scan = {"id": scan_id, "status": "running", "found": [], "completed": 0, "total": len(hosts), "failures": {}, "cancel": False}
            self.scan_auth = (username, password)
        threading.Thread(target=self._scan_network, args=(scan_id, hosts, port, username, password), daemon=True).start()
    def _scan_network(self, scan_id: str, hosts: list[str], port: int, username: str, password: str) -> None:
        discovered = self._ws_discover_hosts()
        hosts = [ip for ip in discovered if ip in hosts] + [ip for ip in hosts if ip not in discovered]
        def inspect(ip: str) -> dict[str, Any] | None:
            try:
                with socket.create_connection((ip, port), timeout=0.4): pass
                from onvif import ONVIFCamera
                from zeep.transports import Transport
                camera = ONVIFCamera(ip, port, username, password, transport=Transport(timeout=2, operation_timeout=5))
                info = camera.devicemgmt.GetDeviceInformation()
                media = camera.create_media_service(); profiles = media.GetProfiles()
                streams = []
                for profile in profiles:
                    uri = media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}, "ProfileToken": profile.token}).Uri
                    streams.append({"name": getattr(profile, "Name", profile.token), "url": uri, **probe_rtsp(uri, username, password)})
                return {"ip": ip, "manufacturer": info.Manufacturer or "", "model": info.Model or "", "streams": streams}
            except Exception as exc:
                with self.scan_lock:
                    if self.scan.get("id") == scan_id and self.scan["status"] == "running": self.scan["failures"][ip] = scrub_message(hide_secret(str(exc), password))[:200]
                return None
        pool = ThreadPoolExecutor(max_workers=16); cancelled = False
        try:
            pending = {pool.submit(inspect, ip) for ip in hosts}
            while pending:
                with self.scan_lock:
                    if self.scan.get("id") != scan_id or self.scan["cancel"]: cancelled = True; break
                done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                for future in done:
                    result = future.result()
                    with self.scan_lock:
                        self.scan["completed"] += 1
                        if result: self.scan["found"].append(result)
        finally: pool.shutdown(wait=not cancelled, cancel_futures=cancelled)
        with self.scan_lock:
            if self.scan.get("id") != scan_id: return
            self.scan["status"] = "cancelled" if self.scan["cancel"] else "complete"
            logger.info("ONVIF 扫描%s：发现=%s，失败=%s", self.scan["status"], len(self.scan["found"]), len(self.scan["failures"]))
    def _ws_discover_hosts(self) -> set[str]:
        message_id = f"uuid:{uuid.uuid4()}"
        probe = (f'<?xml version="1.0" encoding="UTF-8"?><e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header><w:MessageID>{message_id}</w:MessageID><w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To><w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header><e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>').encode("utf-8")
        found: set[str] = set()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            sock.settimeout(0.3)
            sock.sendto(probe, ("239.255.255.250", 3702))
            deadline = time.time() + 2
            while time.time() < deadline:
                try: data, address = sock.recvfrom(65535)
                except socket.timeout: continue
                found.add(address[0])
                for match in re.findall(rb"https?://([0-9.]+)(?::[0-9]+)?/", data):
                    found.add(match.decode("ascii"))
        except OSError as exc:
            logger.warning("WS-Discovery 失败: %s", exc)
        finally:
            sock.close()
        return found
    def add_error(self, message: str) -> None:
        logger.error(message)
        self.status_errors.append({"time": utc_now(), "message": message})
        self.status_errors[:] = self.status_errors[-100:]
    def add_camera(self, body: dict[str, Any]) -> dict[str, Any]:
        main_url = str(body.get("rtsp_url", "")).strip(); preview_url = str(body.get("preview_url", "")).strip() or main_url
        camera_id = str(body.get("id", "")).strip()
        if body.get("scan_ip"):
            with self.scan_lock: found = next((item for item in self.scan["found"] if item["ip"] == body["scan_ip"]), None); username, password = self.scan_auth
            if not found or not found["streams"]: raise AppError("扫描结果已失效", "SCAN_RESULT_MISSING", 409)
            valid = [item for item in found["streams"] if item.get("validated")]
            if not valid: raise AppError("设备没有验证通过的 RTSP 主码流", "NO_VALID_STREAM", 409)
            stream = max(valid, key=lambda item: int(item.get("width") or 0) * int(item.get("height") or 0))
            main_url = authenticated_url(stream["url"], username, password); preview_url = main_url
        camera_id = camera_id or ("cam_" + re.sub(r"[^A-Za-z0-9_-]", "_", urlparse(main_url).hostname or ""))[:40]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", camera_id): raise AppError("摄像头 ID 仅允许字母、数字、下划线和短横线", "INVALID_CAMERA", 422)
        for value in (main_url, preview_url):
            parsed = urlparse(value)
            if parsed.scheme.lower() != "rtsp" or not parsed.hostname:
                raise AppError("RTSP 地址无效", "INVALID_CAMERA", 422)
        config = self.config_store.get()
        camera = {"id": camera_id, "name": str(body.get("name") or camera_id)[:80], "enabled": True, "rtsp_url": main_url, "preview_url": preview_url}
        config["cameras"] = [item for item in config["cameras"] if item["id"] != camera_id] + [camera]
        self.apply_config(config)
        return {**camera, "rtsp_url": scrub_message(main_url), "preview_url": scrub_message(preview_url)}
    def status(self) -> dict[str, Any]:
        usage = shutil.disk_usage(BASE_DIR)
        with self.serial_lock: serials = copy.deepcopy(self.serial_status)
        return {"accepting_events": self.accepting_events, "disk": {"total": usage.total, "free": usage.free, "used": usage.used}, "recorders": {key: value.status for key, value in self.recorders.items()}, "previews": {key: {"mode": value.mode, "viewers": value.viewers} for key, value in self.previews.items()}, "serials": serials, "queues": {key: item.qsize() for key, item in self.queues.items()}, "errors": self.status_errors[-20:]}
    def shutdown(self) -> None:
        logger.info("服务正在退出")
        self.accepting_events = False
        self.stop.set()
        for item in self.recorders.values(): item.close()
        for item in self.previews.values(): item.close()
        self.ocr_client.close()
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()
runtime = Runtime()
app = Flask(__name__, static_folder=None)
csrf_token = secrets.token_urlsafe(32)
@app.before_request
def protect_request() -> None:
    g.request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex)
    host = request.host.split(":", 1)[0].strip("[]")
    if host not in ("127.0.0.1", "localhost"):
        raise AppError("仅允许本机访问", "LOCAL_ONLY", 403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
            raise AppError("Origin 无效", "BAD_ORIGIN", 403)
        if request.headers.get("X-CSRF-Token") != csrf_token:
            raise AppError("CSRF token 无效", "BAD_CSRF", 403)
@app.after_request
def secure_headers(response: Response) -> Response:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'"
    response.headers["X-Request-ID"] = getattr(g, "request_id", "")
    return response
@app.errorhandler(AppError)
def handle_app_error(error: AppError):
    logger.warning("%s: %s", error.code, error.message, extra={"request_id": getattr(g, "request_id", None)})
    return jsonify({"error": {"code": error.code, "message": error.message}, "request_id": getattr(g, "request_id", None)}), error.status
@app.errorhandler(HTTPException)
def handle_http_error(error: HTTPException):
    logger.warning("HTTP %s: %s", error.code, error.name, extra={"request_id": getattr(g, "request_id", None)})
    return jsonify({"error": {"code": f"HTTP_{error.code}", "message": error.description}, "request_id": getattr(g, "request_id", None)}), error.code
@app.errorhandler(Exception)
def handle_error(error: Exception):
    logger.exception("未处理异常", extra={"request_id": getattr(g, "request_id", None)})
    return jsonify({"error": {"code": "INTERNAL_ERROR", "message": "服务器内部错误"}, "request_id": getattr(g, "request_id", None)}), 500
@app.get("/")
def index(): return send_from_directory(RESOURCE_DIR / "web", "index.html")
@app.get("/static/<path:name>")
def static_file(name: str): return send_from_directory(RESOURCE_DIR / "web", name)
@app.get("/api/bootstrap")
def bootstrap(): return jsonify({"csrf_token": csrf_token, "config": runtime.config_store.public(), "status": runtime.status()})
@app.get("/api/health")
def health(): return jsonify({"status": "ok"})
@app.get("/api/ready")
def ready():
    checks = {
        "ffmpeg": FFMPEG.exists(), "ffprobe": FFPROBE.exists(), "writable": os.access(BASE_DIR, os.W_OK),
        "ocr_worker": (OCR_DIR / "ppocr_worker.exe").exists() and (OCR_DIR / "ppocr_worker.exe").stat().st_size > 100_000,
        "ocr_model": (OCR_DIR / "models" / "plate_rtdetr.onnx").exists() and (OCR_DIR / "models" / "plate_rtdetr.onnx").stat().st_size > 1_000_000,
        "ocr_config": (OCR_DIR / "configs" / "OCR.yaml").exists(),
    }
    return jsonify({"status": "ok" if all(checks.values()) else "degraded", "checks": checks}), 200 if all(checks.values()) else 503
@app.get("/api/config")
def get_config(): return jsonify(runtime.config_store.public())
@app.put("/api/config")
def put_config():
    body = request.get_json(silent=False)
    if not isinstance(body, dict) or not isinstance(body.get("config"), dict): raise AppError("请求格式无效", "INVALID_BODY", 422)
    if body.get("secrets") is not None and not isinstance(body["secrets"], dict): raise AppError("敏感配置格式无效", "INVALID_BODY", 422)
    if any(not isinstance(value, str) for value in (body.get("secrets") or {}).values()): raise AppError("敏感配置必须为文本", "INVALID_BODY", 422)
    return jsonify(runtime.apply_config(body["config"], body.get("secrets")))
@app.get("/api/status")
def status(): return jsonify(runtime.status())
@app.get("/api/events")
def events():
    values = list(runtime.manifests.iter_all(200))
    for item in values: item.pop("config", None)
    return jsonify(values)
@app.post("/api/cameras")
def add_camera():
    body = request.get_json(silent=False)
    if not isinstance(body, dict): raise AppError("请求格式无效", "INVALID_BODY", 422)
    return jsonify(runtime.add_camera(body)), 201
@app.post("/api/events/<event_id>/<camera_id>/retry")
def retry(event_id: str, camera_id: str): runtime.retry(event_id, camera_id); return jsonify({"accepted": True}), 202
@app.get("/api/preview/<camera_id>")
def preview(camera_id: str):
    item = runtime.previews.get(camera_id)
    if not item: raise AppError("摄像头不存在", "NOT_FOUND", 404)
    return Response(item.subscribe(), mimetype="multipart/x-mixed-replace; boundary=frame")
@app.post("/api/scan")
def start_scan():
    body = request.get_json(silent=False)
    if not isinstance(body, dict): raise AppError("请求格式无效", "INVALID_BODY", 422)
    try: port = int(body.get("port", 80))
    except (TypeError, ValueError) as exc: raise AppError("ONVIF 端口无效", "INVALID_PORT", 422) from exc
    if not 1 <= port <= 65535: raise AppError("ONVIF 端口无效", "INVALID_PORT", 422)
    runtime.start_scan(str(body.get("subnet", "")), port, str(body.get("username", "")), str(body.get("password", "")))
    return jsonify({"accepted": True}), 202
@app.get("/api/scan")
def scan_status():
    with runtime.scan_lock:
        result = copy.deepcopy(runtime.scan); result.pop("cancel", None)
    for item in result["found"]:
        for stream in item["streams"]: stream.pop("url", None)
    return jsonify(result)
@app.delete("/api/scan")
def cancel_scan():
    with runtime.scan_lock: runtime.scan["cancel"] = True
    return jsonify({"accepted": True})
def set_autostart(enabled: bool) -> None:
    if os.name != "nt": raise AppError("开机自启仅支持 Windows", "WINDOWS_ONLY", 409)
    import winreg
    command = f'"{sys.executable}"' if getattr(sys, "frozen", False) else f'"{sys.executable}" "{Path(__file__).resolve()}"'
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        if enabled: winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, command)
        else:
            try: winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError: pass
@app.put("/api/autostart")
def autostart():
    body = request.get_json(silent=False)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool): raise AppError("请求格式无效", "INVALID_BODY", 422)
    set_autostart(body["enabled"]); return jsonify({"accepted": True})
@app.get("/api/autostart")
def get_autostart(): return jsonify({"enabled": autostart_enabled()})
def acquire_single_instance():
    if os.name != "nt": return None
    import ctypes
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}")
    if ctypes.windll.kernel32.GetLastError() == 183: raise SystemExit("程序已在运行")
    return handle
def tray_thread() -> None:
    try:
        import pystray
        from PIL import Image, ImageDraw
        image = Image.new("RGB", (64, 64), "#0b1220"); draw = ImageDraw.Draw(image)
        draw.ellipse((10, 10, 54, 54), fill="#1fb6ff"); draw.rectangle((27, 20, 37, 44), fill="white")
        def open_web(icon=None, item=None): webbrowser.open(f"http://127.0.0.1:{runtime.config['http']['port']}")
        def toggle(icon=None, item=None): set_autostart(not autostart_enabled())
        def exit_app(icon=None, item=None): runtime.shutdown(); icon.stop(); os._exit(0)
        icon = pystray.Icon(APP_NAME, image, "事件录像系统", pystray.Menu(pystray.MenuItem("打开网页", open_web), pystray.MenuItem("设置/取消开机自启", toggle), pystray.MenuItem("退出程序", exit_app)))
        icon.run()
    except Exception as exc: runtime.add_error(f"托盘启动失败: {exc}")
def autostart_enabled() -> bool:
    if os.name != "nt": return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.QueryValueEx(key, APP_NAME); return True
    except FileNotFoundError: return False
def main() -> None:
    acquire_single_instance()
    runtime.start()
    signal.signal(signal.SIGTERM, lambda *_: runtime.shutdown())
    threading.Thread(target=tray_thread, name="tray", daemon=True).start()
    from waitress import serve
    config = runtime.config["http"]
    logger.info("服务启动 http://%s:%s", config["host"], config["port"])
    try: serve(app, host=config["host"], port=int(config["port"]), threads=4, channel_timeout=120)
    finally: runtime.shutdown()
if __name__ == "__main__": main()
