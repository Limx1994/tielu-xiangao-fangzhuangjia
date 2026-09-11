import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest

os.environ.setdefault("XGFZJ_TESTING", "1")
import app as recorder


@pytest.fixture()
def client():
    recorder.app.config.update(TESTING=True)
    with recorder.app.test_client() as value:
        bootstrap = value.get("/api/bootstrap", headers={"Host": "127.0.0.1"})
        token = bootstrap.get_json()["csrf_token"]
        value.csrf_headers = {"Host": "127.0.0.1", "X-CSRF-Token": token}
        yield value


def test_health_and_security(client):
    assert client.get("/api/health", headers={"Host": "127.0.0.1"}).status_code == 200
    assert client.get("/missing", headers={"Host": "127.0.0.1"}).status_code == 404
    assert client.get("/api/health", headers={"Host": "example.com"}).status_code == 403


def test_config_validation_rejects_bad_values():
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"recording": {"segment_seconds": 1}})
    with pytest.raises(recorder.AppError) as error:
        recorder.validate_config(value)
    assert error.value.code == "INVALID_CONFIG"
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": "bad"})
    with pytest.raises(recorder.AppError) as error: recorder.validate_config(value)
    assert error.value.code == "INVALID_CONFIG"
    camera = {"id": "cam", "rtsp_url": "rtsp://192.0.2.1/main"}
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [{**camera, "id": f"cam{i}"} for i in range(3)]})
    with pytest.raises(recorder.AppError): recorder.validate_config(value)


def test_serial_trigger_parsing():
    assert recorder.parse_trigger({"mode": "hex", "trigger": "AA 55:01"}) == b"\xaa\x55\x01"
    assert recorder.parse_trigger({"mode": "text", "trigger": "OPEN"}) == b"OPEN"
    with pytest.raises(ValueError):
        recorder.parse_trigger({"mode": "hex", "trigger": "ABC"})


def test_serial_probe_protocol_and_config():
    class Stream:
        def __init__(self): self.reply, self.written = recorder.serial_identity("trigger_1"), b""
        @property
        def in_waiting(self): return len(self.reply)
        def reset_input_buffer(self): pass
        def write(self, value): self.written += value
        def flush(self): pass
        def read(self, size): value, self.reply = self.reply[:size], self.reply[size:]; return value
    stream = Stream()
    assert recorder.probe_serial(stream, "trigger_1", 0.1)
    assert stream.written == b"XGFZJ:DISCOVER:1\r\n"
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"serial_ports": [{"device_id": "trigger_1", "baudrate": 9600, "mode": "text", "trigger": "OPEN"}]})
    recorder.validate_config(value)
    value["serial_ports"][0]["device_id"] = "bad id"
    with pytest.raises(recorder.AppError): recorder.validate_config(value)


def test_serial_rebinds_when_com_number_changes(monkeypatch):
    class Stop:
        waits = 0
        def is_set(self): return self.waits >= 2
        def wait(self, _): self.waits += 1
    class Port:
        def __init__(self, device): self.device = device
    scans = iter([[Port("COM7")], [Port("COM19")]])
    opened = []
    class Stream:
        def __init__(self, port, *_args, **_kwargs): self.port, self.chunks = port, [recorder.serial_identity("trigger_1"), b"OPEN"]; opened.append(port)
        @property
        def in_waiting(self): return len(self.chunks[0]) if self.chunks else 1
        def reset_input_buffer(self): pass
        def write(self, _): pass
        def flush(self): pass
        def close(self): pass
        def read(self, _):
            if self.chunks: return self.chunks.pop(0)
            raise OSError("removed")
    monkeypatch.setattr(recorder, "list_ports", type("Ports", (), {"comports": staticmethod(lambda: next(scans))}))
    monkeypatch.setattr(recorder, "serial", type("SerialModule", (), {"Serial": Stream}))
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.stop, runtime.serial_generation = Stop(), 1
    runtime.serial_lock, runtime.serial_scan_lock = threading.Lock(), threading.Lock()
    runtime.serial_claims = {}; runtime.serial_status = {"trigger_1": {}}
    triggered, errors = [], []
    runtime.trigger = triggered.append; runtime.add_error = errors.append
    runtime._serial_loop({"device_id": "trigger_1", "baudrate": 9600, "mode": "text", "trigger": "OPEN"}, 1)
    assert opened == ["COM7", "COM19"]
    assert triggered == ["serial:trigger_1", "serial:trigger_1"]
    assert runtime.serial_claims == {} and len(errors) == 2


def test_normalize_plate():
    assert recorder.normalize_plate(" 粤B·12345 ") == "粤B12345"
    assert recorder.OCRClient.plate_pattern.fullmatch("粤B12345")
    assert recorder.OCRClient.plate_pattern.fullmatch("粤BD12345")
    assert not recorder.OCRClient.plate_pattern.fullmatch("ABC123")


def test_ocr_worker_nested_result(monkeypatch):
    inner = '{"rec_texts":["皖A·195K9"],"rec_scores":[0.9972],}'
    line = "OK " + json.dumps({"results": [{"result": inner}]}, ensure_ascii=False) + "\n"
    class Input:
        def write(self, value): pass
        def flush(self): pass
    class Output:
        def readline(self): return line
    class Process:
        stdin, stdout = Input(), Output()
        def poll(self): return None
        def terminate(self): pass
    client = recorder.OCRClient(recorder.DEFAULT_CONFIG, threading.Event())
    client.process = Process()
    monkeypatch.setattr(client, "ensure_service", lambda: None)
    result = client.recognize(b"jpeg")
    assert result == {"rec_texts": ["皖A·195K9"], "rec_scores": [0.9972]}


def test_atomic_json(tmp_path):
    path = tmp_path / "state" / "value.json"
    recorder.atomic_json(path, {"状态": "完成", "count": 3})
    assert json.loads(path.read_text(encoding="utf-8"))["count"] == 3
    assert not path.with_suffix(".json.tmp").exists()


def test_manifest_iteration_is_limited(tmp_path, monkeypatch):
    monkeypatch.setattr(recorder, "EVENT_DIR", tmp_path)
    store = recorder.ManifestStore()
    for index in range(3):
        path = tmp_path / str(index) / "cam" / "manifest.json"
        recorder.atomic_json(path, {"event_id": str(index), "camera_id": "cam"})
        os.utime(path, (index + 1, index + 1))
    assert [item["event_id"] for item in store.iter_all(2)] == ["2", "1"]


def test_camera_credentials_use_config():
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [{"id": "cam1", "rtsp_url": "rtsp://user:pass@127.0.0.1/live"}]})
    recorder.validate_config(value)
    assert recorder.camera_url(value["cameras"][0], "rtsp_url") == "rtsp://user:pass@127.0.0.1/live"


def test_config_file_secrets_are_masked_and_preserved(tmp_path):
    store = recorder.ConfigStore.__new__(recorder.ConfigStore)
    store.path, store.lock = tmp_path / "config.json", threading.RLock()
    store.data = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"upload": {"token": "secret"}, "cameras": [{"id": "cam1", "rtsp_url": "rtsp://user:pass@127.0.0.1/live"}]})
    public = store.public()
    assert public["upload"]["token"] == "" and public["cameras"][0]["rtsp_url"] == "rtsp://***@127.0.0.1/live"
    public.pop("secret_status")
    store.save(public)
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["upload"]["token"] == "secret" and saved["cameras"][0]["rtsp_url"] == "rtsp://user:pass@127.0.0.1/live"


def test_receipt_contract():
    class FakeResponse:
        status_code = 200
        text = ""
        def json(self):
            return {"accepted": True, "event_id": "evt", "camera_id": "cam", "size": 10, "sha256": "abc"}
    manifest = {"event_id": "evt", "camera_id": "cam"}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    receipt = runtime._check_receipt(FakeResponse(), manifest, True, 10, "abc")
    assert receipt["accepted"] is True
    with pytest.raises(recorder.AppError):
        runtime._check_receipt(FakeResponse(), manifest, True, 11, "abc")


def test_multipart_encoder_streams_file(tmp_path):
    class TrackingStream:
        def __init__(self, stream):
            self.stream, self.bytes_read = stream, 0

        def read(self, size=-1):
            chunk = self.stream.read(size)
            self.bytes_read += len(chunk)
            return chunk

        def tell(self): return self.stream.tell()
        def fileno(self): return self.stream.fileno()

    path = tmp_path / "event.mp4"
    path.write_bytes(b"x" * 1_000_000)
    with path.open("rb") as source:
        stream = TrackingStream(source)
        body = recorder.MultipartEncoder(fields={"metadata": "{}", "file": (path.name, stream, "video/mp4")})
        assert stream.bytes_read == 0
        assert body.read(8192)
        assert 0 < stream.bytes_read < path.stat().st_size


def test_upload_body_enforces_total_timeout():
    body = recorder.DeadlineBody(recorder.MultipartEncoder(fields={"metadata": "{}"}), -1)
    with pytest.raises(recorder.requests.Timeout): body.read(1)


def test_scan_credentials_are_encoded_and_scrubbed():
    value = recorder.authenticated_url("rtsp://192.0.2.1/live", "admin user", "p@ss")
    assert value == "rtsp://admin%20user:p%40ss@192.0.2.1/live"
    assert recorder.scrub_message(value) == "rtsp://***@192.0.2.1/live"
    assert recorder.hide_secret("connection refused", "") == "connection refused"


def test_rtsp_commands_use_supported_timeout():
    camera = {"id": "test", "rtsp_url": "rtsp://192.0.2.1/main"}
    command = recorder.Recorder(camera, recorder.DEFAULT_CONFIG, threading.Event())._command()
    previews = list(recorder.Preview(camera, recorder.DEFAULT_CONFIG)._commands())
    assert "-timeout" in command and "-rw_timeout" not in command
    assert all("-timeout" in item and "-rw_timeout" not in item for _, item in previews)
    assert all("nobuffer" in item for _, item in previews)


def test_scan_result_stores_credentials_in_config():
    saved = {}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.scan_lock, runtime.scan_auth = threading.Lock(), ("admin", "secret")
    runtime.scan = {"found": [{"ip": "192.0.2.1", "streams": [
        {"url": "rtsp://192.0.2.1/broken", "validated": False, "width": None, "height": None},
        {"url": "rtsp://192.0.2.1/sub", "validated": True, "width": 640, "height": 360},
        {"url": "rtsp://192.0.2.1/main", "validated": True, "width": 1920, "height": 1080},
    ]}]}
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": []}})()
    runtime.apply_config = lambda config: saved.update(config)
    camera = runtime.add_camera({"id": "gate_1", "scan_ip": "192.0.2.1"})
    assert "secret" not in json.dumps(camera)
    assert saved["cameras"][0]["rtsp_url"] == "rtsp://admin:secret@192.0.2.1/main"
    assert saved["cameras"][0]["preview_url"] == saved["cameras"][0]["rtsp_url"]


def test_manual_camera_generates_id_and_main_preview():
    saved = {}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": []}})()
    runtime.apply_config = lambda config: saved.update(config)
    camera = runtime.add_camera({"name": "一号门", "rtsp_url": "rtsp://192.0.2.8/main"})
    assert camera["id"] == "cam_192_0_2_8" and saved["cameras"][0]["preview_url"] == "rtsp://192.0.2.8/main"


def test_segment_protection_is_reference_counted(tmp_path):
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.protected, runtime.protected_lock = {}, threading.Lock()
    segment = tmp_path / "shared.ts"
    runtime._protect([segment]); runtime._protect([segment])
    runtime._release([segment])
    assert runtime.protected[segment] == 1
    runtime._release([segment])
    assert segment not in runtime.protected


def test_full_queue_reports_and_releases_key(tmp_path):
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.queue_sets, runtime.queues = {"upload": set()}, {"upload": queue.Queue(maxsize=1)}; runtime.queue_lock = threading.Lock()
    runtime.status_errors = []
    runtime.queues["upload"].put(tmp_path / "occupied.json")
    pending = tmp_path / "pending.json"
    runtime._enqueue("upload", pending)
    assert str(pending) not in runtime.queue_sets["upload"]
    assert "队列已满" in runtime.status_errors[-1]["message"]


def test_upload_stream_and_valid_receipts_delete_file(tmp_path, monkeypatch):
    video = tmp_path / "event.mp4"; video.write_bytes(b"video" * 200_000)
    upload = recorder.deep_merge(recorder.DEFAULT_CONFIG["upload"], {"delete_after_success": True, "text_url": "http://local/text", "video_url": "http://local/video"})
    manifest = {"event_id": "evt", "camera_id": "cam", "trigger_time": 1, "coverage": {}, "ocr": {},
                "config": {"upload": upload}, "video_path": str(video),
                "text_upload": {"status": "pending", "attempts": 0}, "video_upload": {"status": "pending", "attempts": 0}}
    class Store:
        def load(self, path): return manifest
        def save(self, value): pass
    class Response:
        status_code = 200; text = ""
        def __init__(self, receipt): self.receipt = receipt
        def json(self): return self.receipt
    class Session:
        calls = 0
        def close(self): pass
        def post(self, url, **kwargs):
            self.calls += 1
            receipt = {"accepted": True, "event_id": "evt", "camera_id": "cam"}
            if self.calls == 2:
                body = kwargs["data"]
                while body.read(16_384): pass
                receipt.update(size=video.stat().st_size, sha256=recorder.sha256_file(video))
            return Response(receipt)
    monkeypatch.setattr(recorder.requests, "Session", Session)
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store()
    runtime._upload(tmp_path / "manifest.json")
    assert manifest["text_upload"]["status"] == manifest["video_upload"]["status"] == "complete"
    assert not video.exists()


def test_http_retry_classification():
    class Response:
        text = "error"
        def __init__(self, status): self.status_code = status
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    manifest = {"event_id": "evt", "camera_id": "cam", "config": {"upload": {"max_retries": 8}},
                "video_upload": {"attempts": 0}}
    for status, expected in ((302, "attention"), (429, "retry"), (503, "retry"), (401, "attention")):
        manifest["video_upload"] = {"attempts": 0}
        with pytest.raises(recorder.AppError) as error:
            runtime._check_receipt(Response(status), manifest, False)
        runtime._retry_state(manifest, "video_upload", error.value)
        assert manifest["video_upload"]["status"] == expected


def test_text_failure_does_not_block_video(tmp_path, monkeypatch):
    video = tmp_path / "event.mp4"; video.write_bytes(b"video" * 1000)
    upload = recorder.deep_merge(recorder.DEFAULT_CONFIG["upload"], {"delete_after_success": True, "text_url": "http://local/text", "video_url": "http://local/video"})
    manifest = {"event_id": "evt", "camera_id": "cam", "trigger_time": 1, "coverage": {}, "ocr": {},
                "config": {"upload": upload}, "video_path": str(video),
                "text_upload": {"status": "pending", "attempts": 0}, "video_upload": {"status": "pending", "attempts": 0}}
    class Store:
        def load(self, path): return manifest
        def save(self, value): pass
    class Response:
        text = "failure"
        def __init__(self, status, receipt=None): self.status_code, self.receipt = status, receipt
        def json(self): return self.receipt
    class Session:
        calls = 0
        def close(self): pass
        def post(self, url, **kwargs):
            self.calls += 1
            if self.calls == 1: return Response(503)
            body = kwargs["data"]
            while body.read(4096): pass
            return Response(200, {"accepted": True, "event_id": "evt", "camera_id": "cam", "size": video.stat().st_size, "sha256": recorder.sha256_file(video)})
    monkeypatch.setattr(recorder.requests, "Session", Session)
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store()
    runtime._upload(tmp_path / "manifest.json")
    assert manifest["text_upload"]["status"] == "retry"
    assert manifest["video_upload"]["status"] == "complete"
    assert video.exists()


def test_recovery_restores_segment_reference(tmp_path):
    segment = tmp_path / "segment.ts"; segment.write_bytes(b"x")
    manifest = {"event_id": "evt", "camera_id": "cam", "protected_segments": [str(segment)],
                "recording": {"status": "waiting"}, "ocr": {"status": "pending"},
                "coverage": {"requested_end": 0}, "config": {"recording": {"segment_seconds": 2}},
                "text_upload": {"status": "pending"}, "video_upload": {"status": "pending"}}
    class Store:
        def iter_all(self): return iter([manifest])
        def path(self, event_id, camera_id): return tmp_path / "manifest.json"
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.manifests, runtime.protected, runtime.protected_lock = Store(), {}, threading.Lock()
    runtime.queue_sets = {name: set() for name in ("stitch", "ocr", "upload")}; runtime.queue_lock = threading.Lock()
    runtime.queues = {name: queue.Queue(maxsize=2) for name in runtime.queue_sets}; runtime.status_errors = []
    runtime._recover()
    assert runtime.protected[segment] == 1
    assert runtime.queues["stitch"].get_nowait() == tmp_path / "manifest.json"


def test_real_http_json_and_stream_upload(tmp_path, monkeypatch):
    video = tmp_path / "event.mp4"; video.write_bytes(b"stream-data" * 200_000)
    digest, seen = recorder.sha256_file(video), {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args): pass
        def do_POST(self):
            remaining = int(self.headers["Content-Length"]); content = bytearray()
            while remaining:
                chunk = self.rfile.read(min(8192, remaining)); remaining -= len(chunk)
                if self.path == "/text": content.extend(chunk)
            seen[self.path] = {"idempotency": self.headers["Idempotency-Key"], "type": self.headers["Content-Type"]}
            if self.path == "/text":
                assert json.loads(content)["event_id"] == "evt"
                receipt = {"accepted": True, "event_id": "evt", "camera_id": "cam"}
            else:
                assert self.headers["Content-Type"].startswith("multipart/form-data; boundary=")
                receipt = {"accepted": True, "event_id": "evt", "camera_id": "cam", "size": video.stat().st_size, "sha256": digest}
            encoded = json.dumps(receipt).encode(); self.send_response(200)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(encoded)))
            self.end_headers(); self.wfile.write(encoded)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        upload = recorder.deep_merge(recorder.DEFAULT_CONFIG["upload"], {"delete_after_success": False, "text_url": base + "/text", "video_url": base + "/video"})
        manifest = {"event_id": "evt", "camera_id": "cam", "trigger_time": 1, "coverage": {}, "ocr": {},
                    "config": {"upload": upload}, "video_path": str(video),
                    "text_upload": {"status": "pending", "attempts": 0}, "video_upload": {"status": "pending", "attempts": 0}}
        class Store:
            def load(self, path): return manifest
            def save(self, value): pass
        runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store()
        runtime._upload(tmp_path / "manifest.json")
    finally:
        server.shutdown(); server.server_close(); thread.join(5)
    assert manifest["text_upload"]["status"] == manifest["video_upload"]["status"] == "complete"
    assert seen["/text"]["idempotency"].endswith(":text") and seen["/video"]["idempotency"].endswith(":video")


def test_config_api_requires_csrf(client):
    response = client.put("/api/config", json={"config": recorder.DEFAULT_CONFIG}, headers={"Host": "127.0.0.1"})
    assert response.status_code == 403


def test_ocr_zero_frames_is_failure(tmp_path, monkeypatch):
    manifest = {"event_id": "evt", "camera_id": "cam", "video_path": str(tmp_path / "event.mp4"),
                "recording": {"status": "complete"}, "ocr": {"status": "pending"}, "config": recorder.DEFAULT_CONFIG}
    class Store:
        def load(self, path): return manifest
        def save(self, value): pass
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.stop = threading.Event(); runtime.manifests = Store()
    runtime.queue_sets = {name: set() for name in ("ocr", "upload")}; runtime.queue_lock = threading.Lock()
    runtime.queues = {name: queue.Queue() for name in runtime.queue_sets}; runtime.ocr_client = object(); runtime.status_errors = []
    path = tmp_path / "manifest.json"; runtime.queues["ocr"].put(path); runtime.queue_sets["ocr"].add(str(path))
    monkeypatch.setattr(recorder, "jpeg_frames", lambda *_: iter(()))
    worker = threading.Thread(target=runtime._ocr_worker); worker.start()
    for _ in range(50):
        if manifest["ocr"].get("status") != "pending": break
        threading.Event().wait(0.02)
    runtime.stop.set(); worker.join(2)
    assert manifest["ocr"]["status"] == "failed" and manifest["ocr"]["error"] == "未抽取到视频帧"


def test_disk_low_stops_and_restarts_recorders(monkeypatch):
    class Recorder:
        def __init__(self): self.closed = False
        def close(self): self.closed = True
    item = Recorder(); runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.accepting_events, runtime.recorders, runtime.status_errors = True, {"cam": item}, []
    restarted = []; monkeypatch.setattr(runtime, "_start_recorders", lambda: restarted.append(True))
    runtime._set_disk_state(False); runtime._set_disk_state(True)
    assert item.closed and runtime.accepting_events and restarted == [True]


def test_manual_retry_uses_current_upload_config(tmp_path):
    path = tmp_path / "manifest.json"; path.write_text("{}")
    manifest = {"config": {"upload": {"text_url": "old"}}, "text_upload": {"status": "attention", "attempts": 8},
                "video_upload": {"status": "complete", "attempts": 1}}
    class Store:
        def path(self, event_id, camera_id): return path
        def load(self, value): return manifest
        def save(self, value): pass
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store()
    runtime.config = {"upload": {"text_url": "new"}}; runtime.queue_sets = {"upload": set()}; runtime.queue_lock = threading.Lock()
    runtime.queues = {"upload": queue.Queue()}; runtime.retry("evt", "cam")
    assert manifest["config"]["upload"]["text_url"] == "new"
    assert manifest["text_upload"]["status"] == "pending" and manifest["text_upload"]["attempts"] == 0
