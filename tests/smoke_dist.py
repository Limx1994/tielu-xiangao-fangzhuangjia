import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


root = Path(__file__).resolve().parents[1]
if len(sys.argv) > 2:
    raise SystemExit("用法: smoke_dist.py [发布目录]")
dist = Path(sys.argv[1]).resolve() if len(sys.argv) == 2 else root / "dist" / "XgfzjRecorder"
if not (dist / "XgfzjRecorder.exe").is_file():
    raise SystemExit(f"发布程序不存在: {dist / 'XgfzjRecorder.exe'}")
config_path = dist / "config.json"
original_config = config_path.read_bytes() if config_path.exists() else None
base_url = "http://127.0.0.1:5000"
if original_config is not None:
    smoke_config = json.loads(original_config.decode("utf-8-sig")); smoke_config["cameras"] = []; smoke_config["serial_ports"] = []
    base_url = f"http://127.0.0.1:{int(smoke_config['http']['port'])}"
    config_path.write_text(json.dumps(smoke_config, ensure_ascii=False, indent=2), encoding="utf-8")
process = None
try:
    process = subprocess.Popen(
        [str(dist / "XgfzjRecorder.exe")], cwd=dist,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    health = None
    for _ in range(30):
        if process.poll() is not None:
            raise RuntimeError(f"打包程序提前退出，返回码 {process.returncode}；请先退出已运行实例")
        try:
            with urllib.request.urlopen(base_url + "/api/health", timeout=2) as response:
                health = json.load(response)
            break
        except OSError:
            time.sleep(0.5)
    if health != {"status": "ok"}:
        raise RuntimeError("打包程序健康检查失败")
    duplicate = subprocess.Popen([str(dist / "XgfzjRecorder.exe")], cwd=dist, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try: duplicate.wait(timeout=5)
    except subprocess.TimeoutExpired:
        duplicate.kill(); raise RuntimeError("单实例锁未阻止第二个进程") from None
    if duplicate.returncode == 0: raise RuntimeError("第二个进程未报告单实例冲突")
    with urllib.request.urlopen(base_url + "/api/ready", timeout=5) as response:
        ready = json.load(response)
    with urllib.request.urlopen(base_url + "/", timeout=5) as response:
        page = response.read().decode("utf-8")
    if ready["status"] != "ok" or not all(ready["checks"].values()) or "事件录像" not in page:
        raise RuntimeError(f"打包程序就绪检查失败: {ready}")
    print(json.dumps({"health": health, "ready": ready, "page": True}, ensure_ascii=False))
finally:
    if process:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if original_config is None: config_path.unlink(missing_ok=True)
    else: config_path.write_bytes(original_config)
