import http.cookiejar
import json
import subprocess
import time
import urllib.request
import winreg
from pathlib import Path


root = Path(__file__).resolve().parents[1]
dist = root / "dist" / "XgfzjRecorder"
exe = (dist / "XgfzjRecorder.exe").resolve()
run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
name = "XgfzjRecorder"
config = json.loads((dist / "config.json").read_text(encoding="utf-8-sig")) if (dist / "config.json").exists() else {"http": {"port": 5000}}
base_url = f"http://127.0.0.1:{int(config['http']['port'])}"
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def call(method, path, token=None, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Origin": base_url}
    if token: headers["X-CSRF-Token"] = token
    if data: headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    with opener.open(request, timeout=5) as response:
        return json.load(response)


with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as key:
    try: old_value, old_type = winreg.QueryValueEx(key, name)
    except FileNotFoundError: old_value = old_type = None

process = subprocess.Popen([str(exe)], cwd=dist, creationflags=subprocess.CREATE_NO_WINDOW)
try:
    for _ in range(30):
        try:
            token = call("GET", "/api/bootstrap")["csrf_token"]
            break
        except OSError:
            time.sleep(0.5)
    else: raise RuntimeError("打包程序未启动")
    call("PUT", "/api/autostart", token, {"enabled": True})
    assert call("GET", "/api/autostart")["enabled"] is True
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key) as key:
        value, _ = winreg.QueryValueEx(key, name)
    assert value == f'"{exe}"'
    call("PUT", "/api/autostart", token, {"enabled": False})
    assert call("GET", "/api/autostart")["enabled"] is False
    print("开机自启 Registry Run API 验证通过")
finally:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, run_key) as key:
        if old_value is None:
            try: winreg.DeleteValue(key, name)
            except FileNotFoundError: pass
        else: winreg.SetValueEx(key, name, 0, old_type, old_value)
    process.terminate()
    try: process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=5)
