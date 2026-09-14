from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import subprocess
import threading
from typing import Any


def _safe_error(value: str) -> str:
    return re.sub(r"(rtsp://)[^/@\s]+@", r"\1***@", value, flags=re.I)[-1000:]


def _stop_process(process: subprocess.Popen | None) -> None:
    if not process or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


class StreamForwarder:
    def __init__(self, camera: dict[str, Any], ffmpeg: Path, stop: threading.Event, process_options: dict[str, Any]) -> None:
        self.camera, self.ffmpeg, self.stop = camera, ffmpeg, stop
        self.process_options = process_options
        self.closed = threading.Event()
        self.process_lock = threading.Lock()
        self.process: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self.status = {"state": "starting", "target": camera["forward_url"], "restarts": 0, "error": None}

    def _command(self) -> list[str]:
        target = self.camera["forward_url"] + "?pkt_size=1316"
        return [str(self.ffmpeg), "-hide_banner", "-loglevel", "warning", "-rtsp_transport", "tcp", "-timeout", "15000000", "-i", self.camera["rtsp_url"], "-map", "0:v:0", "-an", "-c:v", "copy", "-f", "mpegts", "-mpegts_flags", "+resend_headers", "-muxdelay", "0", "-muxpreload", "0", target]

    def start(self) -> None:
        self.thread = threading.Thread(target=self._run, name=f"forward-{self.camera['id']}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        delay = 1
        while not self.stop.is_set() and not self.closed.is_set():
            process = None
            try:
                self.status.update(state="starting", error=None)
                with self.process_lock:
                    if self.stop.is_set() or self.closed.is_set(): break
                    process = subprocess.Popen(self._command(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **self.process_options)
                    self.process = process
                self.status.update(state="running", error=None)
                errors: deque[str] = deque(maxlen=16)
                if process.stderr:
                    while chunk := process.stderr.read(4096): errors.append(chunk)
                code = process.wait()
                with self.process_lock:
                    if self.process is process: self.process = None
                if self.stop.is_set() or self.closed.is_set():
                    break
                message = _safe_error("".join(errors).strip()) or f"FFmpeg 转发进程意外退出 ({code})"
            except Exception as exc:
                _stop_process(process)
                with self.process_lock:
                    if self.process is process: self.process = None
                if self.stop.is_set() or self.closed.is_set():
                    break
                message = _safe_error(str(exc))
            self.status.update(state="reconnecting", error=message, restarts=self.status["restarts"] + 1)
            if self.closed.wait(delay) or self.stop.is_set():
                break
            delay = min(delay * 2, 30)
        self.status["state"] = "stopped"

    def close(self) -> None:
        self.closed.set()
        with self.process_lock: process = self.process
        _stop_process(process)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=8)
            if self.thread.is_alive(): raise RuntimeError("实时转发线程未能停止")
