import json
import os
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
import stream_forwarder

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


def test_page_exposes_all_config_settings(client):
    page = client.get("/", headers={"Host": "127.0.0.1"}).get_data(as_text=True)
    assert "事件上报地址（URL 或 IP）" in page
    assert "车牌上报地址" not in page
    expected_ids = (
        "config-version", "http-host", "http-port", "recording-pre-seconds",
        "recording-post-seconds", "recording-segment-seconds", "preview-width",
        "preview-fps", "preview-idle-seconds", "ocr-enabled", "ocr-fps",
        "ocr-confidence", "ocr-cpu-threads", "upload-delete-after-success",
        "upload-connect-timeout", "upload-read-timeout", "upload-total-timeout",
        "upload-max-retries", "text-url", "video-url", "api-token",
        "storage-reserve-percent", "storage-reserve-bytes",
        "storage-cache-keep-seconds", "camera-settings", "serial-settings",
    )
    for element_id in expected_ids:
        assert f'id="{element_id}"' in page
    for button_id in ("save-http", "save-recording", "save-preview", "save-ocr", "save-upload", "save-storage"):
        assert f'id="{button_id}"' in page
    assert 'id="config-version" readonly' in page
    assert 'id="config"' not in page and 'id="save-config"' not in page

    script = client.get("/static/app.js", headers={"Host": "127.0.0.1"}).get_data(as_text=True)
    for field in ("id", "name", "enabled", "rtsp_url", "preview_url", "forward_url"):
        assert f'data-camera-field="{field}"' in script
    for field in ("device_id", "baudrate", "mode", "trigger", "enabled"):
        assert f'data-serial-field="{field}"' in script
    assert 'data-camera-field="id" value="${escapeHtml(camera.id)}" readonly' in script
    assert 'data-camera-field="rtsp_url" type="password"' not in script
    assert 'data-camera-field="preview_url" type="password"' not in script
    assert 'id="camera-main" type="password"' not in page
    assert 'id="camera-preview" type="password"' not in page


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
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"upload": {"delete_after_success": "false"}})
    with pytest.raises(recorder.AppError): recorder.validate_config(value)
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"upload": {"text_url": "ftp://192.0.2.1/report"}})
    with pytest.raises(recorder.AppError): recorder.validate_config(value)


@pytest.mark.parametrize("camera", [
    {"id": "cam", "rtsp_url": "rtsp://192.0.2.1:abc/main"},
    {"id": "cam", "rtsp_url": "rtsp://192.0.2.1:70000/main"},
    {"id": "cam", "rtsp_url": "rtsp://192.0.2.1/main", "preview_url": "http://192.0.2.1/sub"},
    {"id": "cam", "rtsp_url": "rtsp://192.0.2.1/main", "preview_url": "rtsp://192.0.2.1:0/sub"},
])
def test_config_validation_rejects_invalid_rtsp_addresses(camera):
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [camera]})
    with pytest.raises(recorder.AppError) as error:
        recorder.validate_config(value)
    assert error.value.code == "INVALID_CONFIG"


def test_config_validation_accepts_all_group_boundaries():
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {
        "http": {"host": "localhost", "port": 65535},
        "recording": {"pre_seconds": 3600, "post_seconds": 600, "segment_seconds": 60},
        "preview": {"width": 1920, "fps": 15, "idle_seconds": 600},
        "ocr": {"enabled": False, "fps": 10, "confidence": 1, "cpu_threads": 16},
        "upload": {"delete_after_success": True, "connect_timeout": 300, "read_timeout": 600,
                   "total_timeout": 86400, "max_retries": 100},
        "storage": {"reserve_percent": 50, "reserve_bytes": 0, "cache_keep_seconds": 86400},
    })
    recorder.validate_config(value)


@pytest.mark.parametrize("update", [
    {"http": {"port": 0}},
    {"preview": {"width": 159}},
    {"preview": {"fps": 16}},
    {"preview": {"idle_seconds": 4}},
    {"ocr": {"enabled": "true"}},
    {"ocr": {"confidence": 1.01}},
    {"ocr": {"fps": 0}},
    {"ocr": {"cpu_threads": 17}},
    {"upload": {"connect_timeout": 0}},
    {"upload": {"read_timeout": 601}},
    {"upload": {"total_timeout": 9}},
    {"upload": {"max_retries": 101}},
    {"storage": {"reserve_percent": 51}},
    {"storage": {"reserve_bytes": -1}},
    {"storage": {"cache_keep_seconds": 29}},
])
def test_config_validation_rejects_all_group_ranges(update):
    with pytest.raises(recorder.AppError) as error:
        recorder.validate_config(recorder.deep_merge(recorder.DEFAULT_CONFIG, update))
    assert error.value.code == "INVALID_CONFIG"


@pytest.mark.parametrize("pre,post", [(0, 0), (3600, 600)])
def test_recording_time_accepts_boundaries(pre, post):
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"recording": {"pre_seconds": pre, "post_seconds": post}})
    recorder.validate_config(value)


@pytest.mark.parametrize("pre,post", [(-1, 20), (3601, 20), (120, -1), (120, 601)])
def test_recording_time_rejects_out_of_range(pre, post):
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"recording": {"pre_seconds": pre, "post_seconds": post}})
    with pytest.raises(recorder.AppError) as error:
        recorder.validate_config(value)
    assert error.value.code == "INVALID_CONFIG"


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


def test_device_ids_and_serial_limit_are_enforced():
    camera = {"id": "same", "rtsp_url": "rtsp://192.0.2.1/main"}
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [camera, dict(camera)]})
    with pytest.raises(recorder.AppError, match="ID 无效或重复"):
        recorder.validate_config(value)
    serial = {"device_id": "same", "baudrate": 9600, "mode": "text", "trigger": "OPEN"}
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"serial_ports": [serial, dict(serial)]})
    with pytest.raises(recorder.AppError, match="串口设备 ID 重复"):
        recorder.validate_config(value)
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"serial_ports": [
        {**serial, "device_id": f"trigger_{index}"} for index in range(17)
    ]})
    with pytest.raises(recorder.AppError, match="设备数量超出限制"):
        recorder.validate_config(value)


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
    store = recorder.ManifestStore(recorder.EVENT_DIR, recorder.atomic_json, recorder.logger, recorder.utc_now)
    for index in range(3):
        path = tmp_path / str(index) / "cam" / "manifest.json"
        recorder.atomic_json(path, {"event_id": str(index), "camera_id": "cam"})
        os.utime(path, (index + 1, index + 1))
    assert [item["event_id"] for item in store.iter_all(2)] == ["2", "1"]
    monkeypatch.setattr(store, "load", lambda _path: pytest.fail("索引后不应重复读取 manifest"))
    assert [item["event_id"] for item in store.iter_all(2)] == ["2", "1"]


def test_manifest_index_keeps_all_active_and_only_200_recent(tmp_path):
    store = recorder.ManifestStore(tmp_path, recorder.atomic_json, recorder.logger, recorder.utc_now)
    base_time = 1_700_000_000_000_000_000
    for index in range(205):
        path = tmp_path / str(index) / "cam" / "manifest.json"
        active = index in (0, 1)
        manifest = {
            "event_id": str(index), "camera_id": "cam", "video_path": str(tmp_path / "missing.mp4"),
            "recording": {"status": "waiting" if active else "complete"},
            "ocr": {"status": "complete"},
            "text_upload": {"status": "complete"}, "video_upload": {"status": "complete"},
            "config": {"upload": {"delete_after_success": False}},
        }
        recorder.atomic_json(path, manifest)
        modified = base_time + index * 1_000_000
        os.utime(path, ns=(modified, modified))
    assert {item["event_id"] for item in store.iter_all()} == {"0", "1"}
    recent = [item["event_id"] for item in store.iter_all(200)]
    assert len(recent) == 200 and recent[0] == "204" and recent[-1] == "5"
    restarted = recorder.ManifestStore(tmp_path, recorder.atomic_json, recorder.logger, recorder.utc_now)
    loads, load = [], restarted.load
    restarted.load = lambda path: loads.append(path) or load(path)
    assert {item["event_id"] for item in restarted.iter_all()} == {"0", "1"}
    assert len(loads) == 202
    restarted.index_path.write_text("{broken", encoding="utf-8")
    fallback = recorder.ManifestStore(tmp_path, recorder.atomic_json, recorder.logger, recorder.utc_now)
    assert {item["event_id"] for item in fallback.iter_all()} == {"0", "1"}


@pytest.mark.parametrize("old_active,new_active", [(False, True), (True, False)])
def test_manifest_index_crash_order_is_recoverable(tmp_path, old_active, new_active):
    def value(active):
        return {"event_id": "evt", "camera_id": "cam", "video_path": str(tmp_path / "missing.mp4"),
                "recording": {"status": "waiting" if active else "complete"}, "ocr": {"status": "complete"},
                "text_upload": {"status": "complete"}, "video_upload": {"status": "complete"},
                "config": {"upload": {"delete_after_success": False}}}
    store = recorder.ManifestStore(tmp_path, recorder.atomic_json, recorder.logger, recorder.utc_now)
    store.save(value(old_active)); calls = 0
    def crash(path, data):
        nonlocal calls
        calls += 1
        if calls == 2: raise OSError("simulated crash")
        recorder.atomic_json(path, data)
    store.atomic_json = crash
    with pytest.raises(OSError): store.save(value(new_active))
    recovered = recorder.ManifestStore(tmp_path, recorder.atomic_json, recorder.logger, recorder.utc_now)
    durable = recorder.ManifestStore._is_active(json.loads(store.path("evt", "cam").read_text(encoding="utf-8")))
    assert bool(list(recovered.iter_all())) is durable


def test_recording_failure_terminates_downstream_and_active_index(tmp_path):
    store = recorder.ManifestStore(tmp_path, recorder.atomic_json, recorder.logger, recorder.utc_now)
    manifest = store.create("evt", {"id": "cam"}, 1, recorder.deep_merge(recorder.DEFAULT_CONFIG, {}))
    manifest["protected_segments"] = [str(tmp_path / "segment.ts")]
    store.save(manifest)
    assert [item["event_id"] for item in store.iter_all()] == ["evt"]
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = store
    runtime._fail_manifest(store.path("evt", "cam"), "recording", recorder.AppError("拼接失败"))
    failed = store.load(store.path("evt", "cam"))
    assert failed["recording"]["status"] == failed["ocr"]["status"] == "failed"
    assert failed["text_upload"]["status"] == failed["video_upload"]["status"] == "attention"
    assert failed["protected_segments"] == [] and list(store.iter_all()) == []


def test_closed_segment_probe_is_cached(tmp_path, monkeypatch):
    segment = tmp_path / "segment.ts"; segment.write_bytes(b"one")
    calls = []
    monkeypatch.setattr(recorder, "probe_segment", lambda path: calls.append(path) or (10.0, ("h264",)))
    item = recorder.Recorder.__new__(recorder.Recorder)
    item.directory, item.process, item.segment_cache = tmp_path, None, {}
    assert item.closed_segments() and item.segment_info(segment)[1] == ("h264",)
    assert item.closed_segments() and calls == [segment]
    segment.write_bytes(b"changed")
    item.segment_info(segment)
    assert calls == [segment, segment]


def test_camera_credentials_use_config():
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [{"id": "cam1", "rtsp_url": "rtsp://user:pass@127.0.0.1/live"}]})
    recorder.validate_config(value)
    assert recorder.camera_url(value["cameras"][0], "rtsp_url") == "rtsp://user:pass@127.0.0.1/live"


def test_config_secrets_visibility_and_preservation(tmp_path):
    store = recorder.ConfigStore.__new__(recorder.ConfigStore)
    store.path, store.lock = tmp_path / "config.json", threading.RLock()
    store.data = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"upload": {"token": "secret"}, "cameras": [{"id": "cam1", "rtsp_url": "rtsp://user:pass@127.0.0.1/live"}]})
    public = store.public()
    assert public["upload"]["token"] == "" and public["cameras"][0]["rtsp_url"] == "rtsp://user:pass@127.0.0.1/live"
    public.pop("secret_status")
    public["cameras"][0]["name"] = "新名称"
    store.save(public)
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["upload"]["token"] == "secret" and saved["cameras"][0]["rtsp_url"] == "rtsp://user:pass@127.0.0.1/live"
    assert saved["cameras"][0]["name"] == "新名称"
    store.save(public, clear_secrets=["token"])
    assert json.loads(store.path.read_text(encoding="utf-8"))["upload"]["token"] == ""


def test_config_store_preserves_extensions_and_rejects_invalid_write(tmp_path):
    store = recorder.ConfigStore.__new__(recorder.ConfigStore)
    store.path, store.lock = tmp_path / "config.json", threading.RLock()
    store.data = recorder.deep_merge(recorder.DEFAULT_CONFIG, {})
    recorder.atomic_json(store.path, store.data)
    incoming = store.public()
    incoming.pop("secret_status")
    incoming["future_extension"] = {"enabled": True}
    saved = store.save(incoming)
    assert saved["future_extension"] == {"enabled": True}
    before = store.path.read_bytes()
    invalid = recorder.deep_merge(saved, {"preview": {"fps": 0}})
    with pytest.raises(recorder.AppError):
        store.save(invalid)
    assert store.path.read_bytes() == before


def test_http_change_is_reported_as_restart_required():
    old = recorder.deep_merge(recorder.DEFAULT_CONFIG, {})
    saved = recorder.deep_merge(old, {"http": {"port": 5001}})
    class Store:
        def save(self, *_args): return saved
        def public(self): return recorder.deep_merge(saved, {})
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_store, runtime.config, runtime.bound_http = Store(), old, old["http"]
    runtime.config_lock = threading.RLock(); runtime.recorders = {}; runtime.previews = {}
    runtime.ocr_client = type("OCR", (), {"config": old, "close": lambda self: None})()
    runtime._start_recorders = lambda: None; runtime._start_serials = lambda: None
    result = runtime.apply_config(saved)
    assert result["restart_required"] is True and runtime.bound_http["port"] == 5000


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


def test_upload_addresses_accept_url_or_bare_host():
    assert recorder.normalize_http_endpoint("192.168.1.20") == "http://192.168.1.20"
    assert recorder.normalize_http_endpoint("192.168.1.20:8080/report") == "http://192.168.1.20:8080/report"
    assert recorder.normalize_http_endpoint("https://example.com/upload") == "https://example.com/upload"
    with pytest.raises(ValueError): recorder.normalize_http_endpoint("192.168.1.999/report")


def test_forward_target_accepts_udp_url_or_ip():
    assert recorder.normalize_udp_target("192.168.2.20") == "udp://192.168.2.20:5000"
    assert recorder.normalize_udp_target("192.168.2.20:5001") == "udp://192.168.2.20:5001"
    assert recorder.normalize_udp_target("udp://receiver.local:6000") == "udp://receiver.local:6000"
    with pytest.raises(ValueError): recorder.normalize_udp_target("http://192.168.2.20:5000")
    with pytest.raises(ValueError): recorder.normalize_udp_target("192.168.2.999:5000")


def test_forward_targets_must_be_unique():
    cameras = [
        {"id": "cam1", "rtsp_url": "rtsp://192.0.2.1/main", "forward_url": "192.168.2.20:5000"},
        {"id": "cam2", "rtsp_url": "rtsp://192.0.2.2/main", "forward_url": "udp://192.168.2.20:5000"},
    ]
    value = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": cameras})
    with pytest.raises(recorder.AppError, match="实时转发目标不能重复"):
        recorder.validate_config(value)


def test_forwarder_uses_stream_copy_and_mpegts():
    camera = {"id": "cam", "rtsp_url": "rtsp://user:pass@192.0.2.1/main", "forward_url": "udp://192.168.2.20:5000"}
    command = recorder.StreamForwarder(camera, recorder.FFMPEG, threading.Event(), {})._command()
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-f") + 1] == "mpegts"
    assert command[-1] == "udp://192.168.2.20:5000?pkt_size=1316"


def test_forwarder_bounds_stderr_and_close_stops_thread(monkeypatch):
    created, limits = threading.Event(), []
    original_deque = stream_forwarder.deque

    def bounded_deque(*args, **kwargs):
        value = original_deque(*args, **kwargs); limits.append(value.maxlen); return value

    class ErrorPipe:
        def __init__(self, process): self.process = process
        def read(self, _size): self.process.terminated.wait(2); return ""

    class Process:
        def __init__(self):
            self.terminated = threading.Event(); self.stderr = ErrorPipe(self); created.set()
        def poll(self): return 0 if self.terminated.is_set() else None
        def terminate(self): self.terminated.set()
        def wait(self, timeout=None): return 0
        def kill(self): self.terminated.set()

    monkeypatch.setattr(stream_forwarder, "deque", bounded_deque)
    monkeypatch.setattr(stream_forwarder.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    forwarder = recorder.StreamForwarder(
        {"id": "cam", "rtsp_url": "rtsp://192.0.2.1/main", "forward_url": "udp://192.0.2.2:5000"},
        recorder.FFMPEG, threading.Event(), {},
    )
    forwarder.start()
    assert created.wait(1)
    forwarder.close()
    assert limits == [16] and not forwarder.thread.is_alive()
    assert forwarder.status["state"] == "stopped"


def test_config_store_normalizes_bare_upload_addresses(tmp_path):
    store = recorder.ConfigStore.__new__(recorder.ConfigStore)
    store.path, store.lock = tmp_path / "config.json", threading.RLock()
    store.data = recorder.deep_merge(recorder.DEFAULT_CONFIG, {})
    saved = store.save(recorder.DEFAULT_CONFIG, {"text_url": "192.168.1.20/report", "video_url": "192.168.1.21:8080/video"})
    assert saved["upload"]["text_url"] == "http://192.168.1.20/report"
    assert saved["upload"]["video_url"] == "http://192.168.1.21:8080/video"


def test_config_store_normalizes_forward_target(tmp_path):
    store = recorder.ConfigStore.__new__(recorder.ConfigStore)
    store.path, store.lock = tmp_path / "config.json", threading.RLock()
    store.data = recorder.deep_merge(recorder.DEFAULT_CONFIG, {})
    incoming = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [{"id": "cam1", "rtsp_url": "rtsp://192.0.2.1/main", "forward_url": "192.168.2.20:5001"}]})
    saved = store.save(incoming)
    assert saved["cameras"][0]["forward_url"] == "udp://192.168.2.20:5001"


def test_network_clients_ignore_proxy_environment(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    environment = recorder.process_args()["env"]
    assert not any(key.upper().endswith("_PROXY") and key.upper() != "NO_PROXY" for key in environment)
    assert environment["NO_PROXY"] == environment["no_proxy"] == "*"
    with recorder.direct_session() as session:
        assert session.trust_env is False and session.proxies == {}


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
    camera = runtime.add_camera({"id": "gate_1", "scan_ip": "192.0.2.1", "forward_url": "192.168.2.20:5000"})
    assert "secret" not in json.dumps(camera)
    assert saved["cameras"][0]["rtsp_url"] == "rtsp://admin:secret@192.0.2.1/main"
    assert saved["cameras"][0]["preview_url"] == saved["cameras"][0]["rtsp_url"]
    assert saved["cameras"][0]["forward_url"] == "udp://192.168.2.20:5000"


def test_manual_camera_generates_id_and_main_preview():
    saved = {}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": []}})()
    runtime.apply_config = lambda config: saved.update(config)
    camera = runtime.add_camera({"name": "一号门", "rtsp_url": "rtsp://192.0.2.8/main", "forward_url": "udp://192.168.2.20:5000"})
    assert camera["id"] == "cam_192_0_2_8" and saved["cameras"][0]["preview_url"] == "rtsp://192.0.2.8/main"
    assert saved["cameras"][0]["forward_url"] == "udp://192.168.2.20:5000"


def test_manual_camera_accepts_custom_id_and_preview():
    saved = {}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": []}})()
    runtime.apply_config = lambda config: saved.update(config)
    camera = runtime.add_camera({"id": "gate_1", "rtsp_url": "rtsp://192.0.2.8/main",
                                 "preview_url": "rtsp://192.0.2.8/sub"})
    assert camera["id"] == "gate_1" and saved["cameras"][0]["preview_url"] == "rtsp://192.0.2.8/sub"


def test_manual_camera_rejects_invalid_forward_target():
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": []}})()
    with pytest.raises(recorder.AppError) as error:
        runtime.add_camera({"rtsp_url": "rtsp://192.0.2.8/main", "forward_url": "http://192.168.2.20:5000"})
    assert error.value.status == 422 and error.value.code == "INVALID_CAMERA"


def test_manual_camera_ids_distinguish_streams():
    state = {"cameras": []}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": list(state["cameras"])}})()
    runtime.apply_config = lambda config: state.update(config) or config
    first = runtime.add_camera({"rtsp_url": "rtsp://192.0.2.8/channel1"})
    second = runtime.add_camera({"rtsp_url": "rtsp://192.0.2.8/channel2"})
    assert first["id"] != second["id"] and len(state["cameras"]) == 2


def test_remove_camera_updates_config():
    saved = {}
    cameras = [{"id": "cam1"}, {"id": "cam2"}]
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_lock = threading.RLock()
    runtime.config_store = type("Store", (), {"get": lambda self: {"cameras": cameras}})()
    runtime.manifests = type("Manifests", (), {"iter_all": lambda self: iter(())})()
    runtime.apply_config = lambda config: saved.update(config) or config
    result = runtime.remove_camera("cam1")
    assert result["cameras"] == [{"id": "cam2"}]
    with pytest.raises(recorder.AppError) as error: runtime.remove_camera("missing")
    assert error.value.code == "NOT_FOUND"


def test_remove_camera_rejects_pending_event():
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.config_lock = threading.RLock()
    runtime.config = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [{"id": "cam1", "rtsp_url": "rtsp://192.0.2.1/main"}]})
    runtime.config_store = type("Store", (), {"get": lambda self: runtime.config, "save": lambda *_: pytest.fail("忙碌摄像头配置不应保存")})()
    runtime.manifests = type("Manifests", (), {"iter_all": lambda self: iter([{"camera_id": "cam1", "recording": {"status": "waiting"}}])})()
    updated = recorder.deep_merge(runtime.config, {"cameras": []})
    with pytest.raises(recorder.AppError) as error: runtime.apply_config(updated)
    assert error.value.code == "CAMERA_BUSY"


@pytest.mark.parametrize("camera_update", [
    {"enabled": False},
    {"rtsp_url": "rtsp://192.0.2.2/main"},
])
def test_pending_event_rejects_camera_disable_or_stream_change(camera_update):
    camera = {"id": "cam1", "enabled": True, "rtsp_url": "rtsp://192.0.2.1/main"}
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_lock = threading.RLock()
    runtime.config = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": [camera]})
    runtime.manifests = type("Manifests", (), {"iter_all": lambda self: [{
        "camera_id": "cam1", "recording": {"status": "waiting"},
    }]})()
    runtime.config_store = type("Store", (), {
        "save": lambda *_args: pytest.fail("等待拼接时不得保存破坏主流的配置"),
    })()
    updated = recorder.deep_merge(runtime.config, {"cameras": [{**camera, **camera_update}]})
    with pytest.raises(recorder.AppError) as error:
        runtime.apply_config(updated)
    assert error.value.code == "CAMERA_BUSY"


def test_preview_close_terminates_existing_subscriber():
    class ExistingThread:
        @staticmethod
        def is_alive(): return True

    preview = recorder.Preview({"id": "cam", "rtsp_url": "rtsp://192.0.2.1/main"}, recorder.DEFAULT_CONFIG)
    preview.thread = ExistingThread()
    preview.frame = b"jpeg"
    subscriber = preview.subscribe()
    assert next(subscriber).endswith(b"jpeg\r\n")
    preview.close()
    with pytest.raises(StopIteration):
        next(subscriber)
    assert preview.viewers == 0


def test_ocr_stop_prevents_new_process(monkeypatch):
    stop = threading.Event(); stop.set()
    client = recorder.OCRClient(recorder.DEFAULT_CONFIG, stop)
    monkeypatch.setattr(recorder.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("退出后不得启动 OCR"))
    with pytest.raises(recorder.AppError) as error:
        client.ensure_service()
    assert error.value.code == "SHUTTING_DOWN"


def test_ocr_cpu_threads_change_restarts_process(tmp_path, monkeypatch):
    ocr_dir, log_dir = tmp_path / "ocr", tmp_path / "logs"
    (ocr_dir / "models").mkdir(parents=True); log_dir.mkdir()
    (ocr_dir / "ppocr_worker.exe").write_bytes(b"x" * 100_001)
    (ocr_dir / "models" / "plate_rtdetr.onnx").write_bytes(b"x" * 1_000_001)
    processes = []

    class Output:
        @staticmethod
        def readline(): return "READY\n"

    class Process:
        stdout = Output()
        def __init__(self, args): self.args, self.terminated = args, False
        def poll(self): return 0 if self.terminated else None
        def terminate(self): self.terminated = True
        def wait(self, timeout=None): return 0
        def kill(self): self.terminated = True

    def popen(args, **_kwargs):
        process = Process(args); processes.append(process); return process

    monkeypatch.setattr(recorder, "OCR_DIR", ocr_dir)
    monkeypatch.setattr(recorder, "LOG_DIR", log_dir)
    monkeypatch.setattr(recorder.subprocess, "Popen", popen)
    config = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"ocr": {"cpu_threads": 2}})
    client = recorder.OCRClient(config, threading.Event())
    client.ensure_service()
    client.config = recorder.deep_merge(config, {"ocr": {"cpu_threads": 6}})
    client.ensure_service()
    assert len(processes) == 2 and processes[0].terminated
    assert processes[0].args[-4:-2] == ["--cpu_threads", "2"]
    assert processes[1].args[-4:-2] == ["--cpu_threads", "6"]
    client.close()


def test_remove_camera_api(client, monkeypatch):
    removed = []
    monkeypatch.setattr(recorder.runtime, "remove_camera", lambda camera_id: removed.append(camera_id) or {"cameras": []})
    response = client.delete("/api/cameras/cam_1", headers=client.csrf_headers)
    assert response.status_code == 200 and response.get_json() == {"cameras": []}
    assert removed == ["cam_1"]


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


@pytest.mark.parametrize("video_url", ["", "http://local/video"])
def test_text_upload_succeeds_without_video_endpoint_or_file(tmp_path, monkeypatch, video_url):
    upload = recorder.deep_merge(recorder.DEFAULT_CONFIG["upload"], {
        "text_url": "http://local/text", "video_url": video_url,
    })
    manifest = {
        "event_id": "evt", "camera_id": "cam", "trigger_time": 1, "coverage": {}, "ocr": {},
        "config": {"upload": upload}, "video_path": str(tmp_path / "missing.mp4"),
        "text_upload": {"status": "pending", "attempts": 0},
        "video_upload": {"status": "pending", "attempts": 0},
    }
    calls = []

    class Store:
        def load(self, _path): return manifest
        def save(self, _value): pass

    class Response:
        status_code, text = 200, ""
        @staticmethod
        def json(): return {"accepted": True, "event_id": "evt", "camera_id": "cam"}

    class Session:
        def close(self): pass
        def post(self, url, **_kwargs): calls.append(url); return Response()

    monkeypatch.setattr(recorder.requests, "Session", Session)
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store()
    runtime._upload(tmp_path / "manifest.json")
    assert calls == ["http://local/text"] and manifest["text_upload"]["status"] == "complete"
    assert manifest["video_upload"]["status"] == "attention"


def test_video_upload_reuses_cached_file_identity(tmp_path, monkeypatch):
    video = tmp_path / "event.mp4"; video.write_bytes(b"video")
    stat = video.stat(); digest = "cached-digest"
    upload = recorder.deep_merge(recorder.DEFAULT_CONFIG["upload"], {
        "text_url": "", "video_url": "http://local/video",
    })
    manifest = {
        "event_id": "evt", "camera_id": "cam", "trigger_time": 1, "coverage": {}, "ocr": {},
        "config": {"upload": upload}, "video_path": str(video),
        "video_file": {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": digest},
        "text_upload": {"status": "complete", "attempts": 0},
        "video_upload": {"status": "pending", "attempts": 0},
    }

    class Store:
        def load(self, _path): return manifest
        def save(self, _value): pass

    class Response:
        status_code, text = 200, ""
        @staticmethod
        def json():
            return {"accepted": True, "event_id": "evt", "camera_id": "cam",
                    "size": stat.st_size, "sha256": digest}

    class Session:
        def close(self): pass
        def post(self, _url, **_kwargs): return Response()

    monkeypatch.setattr(recorder.requests, "Session", Session)
    monkeypatch.setattr(recorder, "sha256_file", lambda _path: pytest.fail("文件身份未变化时不应重复计算摘要"))
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store()
    runtime._upload(tmp_path / "manifest.json")
    assert manifest["video_upload"]["status"] == "complete"


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
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
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


def test_config_api_forwards_secret_clear(client, monkeypatch):
    seen = {}
    def apply(config, secrets, clear):
        seen.update(config=config, secrets=secrets, clear=clear)
        return {"restart_required": False}
    monkeypatch.setattr(recorder.runtime, "apply_config", apply)
    body = {"config": recorder.DEFAULT_CONFIG, "secrets": {"token": ""}, "clear_secrets": ["token"]}
    response = client.put("/api/config", json=body, headers=client.csrf_headers)
    assert response.status_code == 200 and seen["clear"] == ["token"]


def test_tray_autostart_shows_state_and_feedback(monkeypatch):
    import pystray
    state = {"enabled": False}
    captured, updates, notices = {}, [], []
    monkeypatch.setattr(recorder, "autostart_enabled", lambda: state["enabled"])
    monkeypatch.setattr(recorder, "set_autostart", lambda enabled: state.update(enabled=enabled))
    monkeypatch.setattr(pystray.Icon, "run", lambda self: captured.update(icon=self))
    monkeypatch.setattr(pystray.Icon, "update_menu", lambda self: updates.append(True))
    monkeypatch.setattr(pystray.Icon, "notify", lambda self, message, title=None: notices.append((message, title)))
    recorder.tray_thread()
    icon = captured["icon"]
    item = list(icon.menu)[1]
    assert item.text == "开机自启：未开启" and item.checked is False
    item(icon)
    assert item.text == "开机自启：已开启" and item.checked is True
    assert updates == [True] and notices == [("开机自启已开启", "事件录像系统")]


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


def test_old_enabled_ocr_task_closes_worker_when_current_config_disabled(tmp_path, monkeypatch):
    old_config = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"ocr": {"enabled": True}})
    current_config = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"ocr": {"enabled": False}})
    manifest = {
        "event_id": "evt", "camera_id": "cam", "video_path": str(tmp_path / "event.mp4"),
        "recording": {"status": "complete"}, "ocr": {"status": "pending"}, "config": old_config,
    }
    closed, recognized = threading.Event(), []

    class Store:
        def load(self, _path): return manifest
        def save(self, _value): pass

    class OCR:
        def __init__(self): self.config, self.lock = current_config, threading.RLock()
        def recognize(self, _jpeg, config): recognized.append(config); return {"rec_texts": [], "rec_scores": []}
        def close(self): closed.set()

    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.stop, runtime.manifests, runtime.ocr_client = threading.Event(), Store(), OCR()
    runtime.queue_sets = {name: set() for name in ("ocr", "upload")}; runtime.queue_lock = threading.Lock()
    runtime.queues = {name: queue.Queue() for name in runtime.queue_sets}; runtime.status_errors = []
    path = tmp_path / "manifest.json"; runtime.queues["ocr"].put(path); runtime.queue_sets["ocr"].add(str(path))
    monkeypatch.setattr(recorder, "jpeg_frames", lambda *_args: iter([(0.0, b"jpeg")]))
    worker = threading.Thread(target=runtime._ocr_worker); worker.start()
    assert closed.wait(2)
    runtime.stop.set(); worker.join(2)
    assert recognized == [old_config] and manifest["ocr"]["status"] == "no_plate"
    assert not worker.is_alive()


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


def test_active_upload_retry_is_deferred_then_requeued(tmp_path):
    path = tmp_path / "evt" / "cam" / "manifest.json"
    path.parent.mkdir(parents=True); path.write_text("{}", encoding="utf-8")
    manifest = {
        "config": {"upload": {"text_url": "old"}},
        "text_upload": {"status": "attention", "attempts": 8},
        "video_upload": {"status": "complete", "attempts": 1},
    }

    class Store:
        def path(self, _event_id, _camera_id): return path
        def load(self, _path): return manifest
        def save(self, _value): pass

    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.manifests, runtime.config = Store(), {"upload": {"text_url": "new"}}
    runtime.stop, runtime.queue_lock = threading.Event(), threading.Lock()
    runtime.queue_sets, runtime.upload_reruns = {"upload": {str(path)}}, set()
    runtime.queues, runtime.status_errors = {"upload": queue.Queue()}, []
    runtime.queues["upload"].put(path); assert runtime.queues["upload"].get_nowait() == path
    runtime.retry("evt", "cam")
    assert str(path) in runtime.upload_reruns and runtime.queues["upload"].empty()
    manifest["text_upload"]["status"] = "complete"
    runtime._done_queue("upload", path)
    assert runtime.queues["upload"].empty()
    assert manifest["config"]["upload"]["text_url"] == "old"
    assert manifest["text_upload"]["status"] == "complete"


def test_full_upload_queue_uses_bounded_scheduler_and_restores_retry(tmp_path):
    path = tmp_path / "evt" / "cam" / "manifest.json"; path.parent.mkdir(parents=True); path.write_text("{}")
    manifest = {"config": {"upload": {"text_url": "old"}}, "text_upload": {"status": "attention", "attempts": 1},
                "video_upload": {"status": "complete", "attempts": 1}}
    class Store:
        def path(self, *_args): return path
        def load(self, _path): return manifest
        def save(self, _value): pass
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.manifests = Store(); runtime.config = {"upload": {"text_url": "new"}}
    runtime.stop = threading.Event(); runtime.queue_lock = threading.RLock(); runtime.delay_condition = threading.Condition(runtime.queue_lock)
    runtime.queue_sets = {"upload": set()}; runtime.queues = {"upload": queue.Queue(maxsize=1)}; runtime.queues["upload"].put(tmp_path / "busy")
    runtime.delayed, runtime.delay_seq, runtime.delay_limit, runtime.upload_reruns, runtime.status_errors = [], 0, 0, {}, []
    with pytest.raises(recorder.AppError): runtime.retry("evt", "cam")
    assert manifest["text_upload"]["status"] == "retry" and manifest["text_upload"]["next_retry"] > 0
    runtime.queues["upload"].get_nowait(); runtime.delay_limit = 1
    assert runtime._enqueue("upload", path) and runtime.queues["upload"].get_nowait() == path


def test_delayed_tasks_use_one_bounded_scheduler_and_shutdown(tmp_path):
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.stop = threading.Event(); runtime.queue_lock = threading.RLock()
    runtime.delay_condition = threading.Condition(runtime.queue_lock); runtime.delayed, runtime.delay_seq, runtime.delay_limit = [], 0, 32
    runtime.queue_sets = {"stitch": set()}; runtime.queues = {"stitch": queue.Queue()}; runtime.status_errors = []
    runtime.delay_thread = threading.Thread(target=runtime._delay_worker, name="worker-delay", daemon=True); runtime.delay_thread.start()
    try:
        assert all(runtime._enqueue("stitch", tmp_path / str(index), 60) for index in range(32))
        assert not runtime._enqueue("stitch", tmp_path / "overflow", 60)
        assert len(runtime.delayed) == 32 and sum(item.name == "worker-delay" for item in threading.enumerate()) == 1
    finally:
        runtime.stop.set()
        with runtime.delay_condition: runtime.delay_condition.notify_all()
        runtime.delay_thread.join(2)
    assert not runtime.delay_thread.is_alive() and "threading.Timer" not in (recorder.BASE_DIR / "app.py").read_text(encoding="utf-8")

def test_maintenance_reschedules_stranded_ocr_and_upload_pending(tmp_path, monkeypatch):
    paths = {name: tmp_path / name / "cam" / "manifest.json" for name in ("ocr", "upload")}
    manifests = [
        {"event_id": "ocr", "camera_id": "cam", "recording": {"status": "complete"}, "ocr": {"status": "pending"}, "text_upload": {"status": "pending"}, "video_upload": {"status": "pending"}, "config": recorder.DEFAULT_CONFIG},
        {"event_id": "upload", "camera_id": "cam", "recording": {"status": "complete"}, "ocr": {"status": "complete"}, "text_upload": {"status": "pending"}, "video_upload": {"status": "complete"}, "config": recorder.DEFAULT_CONFIG},
    ]
    class Store:
        def iter_all(self): return manifests
        def path(self, event_id, _camera_id): return paths[event_id]
    class Stop:
        calls = 0
        def wait(self, _seconds): self.calls += 1; return self.calls > 1
    runtime = recorder.Runtime.__new__(recorder.Runtime); runtime.stop = Stop(); runtime.manifests = Store()
    runtime.config = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"storage": {"reserve_percent": 0, "reserve_bytes": 0}})
    runtime.queue_lock = threading.RLock(); runtime.delay_condition = threading.Condition(runtime.queue_lock)
    runtime.queue_sets = {name: set() for name in ("stitch", "ocr", "upload")}; runtime.queues = {name: queue.Queue(maxsize=1) for name in runtime.queue_sets}
    runtime.delayed, runtime.delay_seq, runtime.delay_limit, runtime.status_errors = [], 0, 0, []
    for stage in ("ocr", "upload"):
        runtime.queues[stage].put(tmp_path / "busy"); assert not runtime._enqueue(stage, paths[stage]); runtime.queues[stage].get_nowait()
    runtime.accepting_events, runtime.recorders, runtime.protected, runtime.protected_lock = True, {}, {}, threading.Lock()
    monkeypatch.setattr(recorder, "CACHE_DIR", tmp_path / "cache"); (tmp_path / "cache").mkdir()
    runtime._maintenance()
    assert runtime.queues["ocr"].get_nowait() == paths["ocr"] and runtime.queues["upload"].get_nowait() == paths["upload"]


def test_config_change_rebuilds_only_dependent_camera_task():
    cameras = [
        {"id": "cam1", "enabled": True, "rtsp_url": "rtsp://192.0.2.1/main",
         "preview_url": "rtsp://192.0.2.1/sub", "forward_url": "udp://192.0.2.10:5000"},
        {"id": "cam2", "enabled": True, "rtsp_url": "rtsp://192.0.2.2/main",
         "preview_url": "rtsp://192.0.2.2/sub", "forward_url": "udp://192.0.2.11:5000"},
    ]
    old = recorder.deep_merge(recorder.DEFAULT_CONFIG, {"cameras": cameras})
    updated = recorder.deep_merge(old, {"cameras": [
        {**cameras[0], "preview_url": "rtsp://192.0.2.1/new-sub"}, cameras[1],
    ]})
    closed = []

    class Task:
        def __init__(self, name): self.name, self.closed = name, threading.Event()
        def close(self): self.closed.set(); closed.append(self.name)

    class Store:
        def __init__(self): self.saved = old
        def save(self, value, *_args): self.saved = value; return value
        def public(self): return recorder.deep_merge(self.saved, {})

    class OCR:
        def __init__(self): self.lock, self.config = threading.RLock(), old
        def close(self): closed.append("ocr")

    recorder1, recorder2 = Task("recorder1"), Task("recorder2")
    preview1, preview2 = Task("preview1"), Task("preview2")
    forward1, forward2 = Task("forward1"), Task("forward2")
    runtime = recorder.Runtime.__new__(recorder.Runtime)
    runtime.config_lock, runtime.config_store, runtime.config = threading.RLock(), Store(), old
    runtime.bound_http, runtime.manifests = old["http"], type("Manifests", (), {"iter_all": lambda self: []})()
    runtime.recorders = {"cam1": recorder1, "cam2": recorder2}
    runtime.previews = {"cam1": preview1, "cam2": preview2}
    runtime.forwarders = {"cam1": forward1, "cam2": forward2}
    runtime.accepting_events, runtime.stop, runtime.ocr_client = True, threading.Event(), OCR()
    runtime.apply_config(updated)
    assert closed == ["preview1"]
    assert runtime.recorders == {"cam1": recorder1, "cam2": recorder2}
    assert runtime.previews["cam2"] is preview2 and runtime.previews["cam1"] is not preview1
    assert runtime.forwarders == {"cam1": forward1, "cam2": forward2}


def test_frontend_serializes_all_config_mutations():
    script = (recorder.BASE_DIR / "web" / "app.js").read_text(encoding="utf-8")
    assert "let configSave=Promise.resolve()" in script
    assert "function serializeConfigSave(action)" in script
    assert "return serializeConfigSave(async()=>" in script
    for action in ("removeCamera", '$("camera-add").onclick', '$("scan-results").onclick'):
        start = script.index(action)
        assert "serializeConfigSave(async()=>" in script[start:start + 1200]
    assert "data-camera-index" not in script and 'closest("[data-camera-id]")' in script
    assert "value.cameras.find(item=>item.id===cameraId)" in script and "配置已变化，请重试" in script; scan = script[script.index('$("scan-results").onclick'):]; assert scan.index("const items=") < scan.index("await serializeConfigSave")


def test_build_excludes_unused_ocr_service_and_rebuilds_checksums():
    script = (recorder.BASE_DIR / "scripts" / "build.ps1").read_text(encoding="utf-8")
    assert '$_.Name -notin @("ppocr_service.exe", "SHA256SUMS.json")' in script
    assert "$releaseFiles = @(Get-ChildItem -LiteralPath $ocrTarget -Recurse -File" in script
    assert "$releaseChecksums | ConvertTo-Json" in script
    assert "发布目录 OCR 校验失败" in script
    assert "--distpath $stageRoot --workpath $stageWork" in script and "tests\\smoke_dist.py $stageTarget" in script
    assert "Move-DistTree $target $rollback" in script and "Move-DistTree $rollback $target" in script
