import argparse
import http.cookiejar
import io
import json
import time
import urllib.request

from PIL import Image


opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(base, method, path, token=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Origin": base}
    if token: headers["X-CSRF-Token"] = token
    if data: headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with opener.open(request, timeout=10) as response:
        return json.load(response)


parser = argparse.ArgumentParser(description="通过通用 ONVIF 流程连接并验证摄像头预览")
parser.add_argument("--subnet", required=True)
parser.add_argument("--port", type=int, default=80)
parser.add_argument("--username", default="")
parser.add_argument("--password", default="")
parser.add_argument("--base", default="http://127.0.0.1:5000")
args = parser.parse_args()

bootstrap = call(args.base, "GET", "/api/bootstrap")
token = bootstrap["csrf_token"]
call(args.base, "POST", "/api/scan", token, {"subnet": args.subnet, "port": args.port, "username": args.username, "password": args.password})
deadline = time.time() + 60
while time.time() < deadline:
    scan = call(args.base, "GET", "/api/scan")
    if scan["status"] != "running": break
    time.sleep(0.5)
else: raise RuntimeError("ONVIF 扫描超时")

device = next((item for item in scan["found"] if any(stream["validated"] for stream in item["streams"])), None)
if not device: raise RuntimeError(f"没有发现可连接设备，失败数: {len(scan['failures'])}")
camera_id = "cam_" + device["ip"].replace(".", "_")
camera = call(args.base, "POST", "/api/cameras", token, {"id": camera_id, "name": f"ONVIF {device['ip']}", "scan_ip": device["ip"]})

preview = opener.open(args.base + "/api/preview/" + camera_id, timeout=30)
buffer = bytearray(); frame = None; deadline = time.time() + 30
while time.time() < deadline:
    buffer.extend(preview.read(8192))
    start, end = buffer.find(b"\xff\xd8"), buffer.find(b"\xff\xd9")
    if start >= 0 and end > start:
        frame = bytes(buffer[start:end + 2]); break
preview.close()
if not frame: raise RuntimeError("30 秒内未收到完整 MJPEG 帧")
with Image.open(io.BytesIO(frame)) as image:
    image.verify(); size = image.size
status = call(args.base, "GET", "/api/status")
print(json.dumps({"camera": camera, "device": device, "jpeg_bytes": len(frame), "jpeg_size": size,
                  "recorder": status["recorders"].get(camera_id), "preview": status["previews"].get(camera_id)}, ensure_ascii=False))
