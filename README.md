# 嵌入式 Windows 事件录像系统

面向 Windows 10/11 x64 工控机的本地事件录像程序。串口收到指定指令后，系统无损拼接触发前后的录像切片，抽帧调用本机 PaddleOCR worker 识别车牌，并可靠上传识别结果和视频。

默认参数为触发前 120 秒、触发后 20 秒、每段 10 秒、OCR 1 FPS。目标设备为 4 GB 内存的 NUC 类工控机，建议最多接入 2 路摄像头。

## 主要功能

- 使用 FFmpeg Stream Copy 循环录像并拼接事件视频，避免重复编码。
- 通过 ONVIF 搜索摄像头，也支持手动填写 RTSP 地址。
- 自动识别串口设备身份，COM 号变化后自动重新绑定。
- 使用常驻 `ppocr_worker.exe` 完成本地 OCR，并按中国大陆车牌格式过滤结果。
- 文本与视频独立重试，使用幂等键和严格回执校验防止误删本地文件。
- 提供本机网页管理、实时预览、事件查询、手动重试和开机自启控制。
- 磁盘空间不足时暂停新事件和录像缓存，空间恢复后自动重启录像。

## 运行要求

- Windows 10/11 x64。
- 开发运行需要 Python 及 `requirements.txt` 中的依赖；当前验证环境为 Python 3.14.6。
- 摄像头应提供可访问的 RTSP 主码流；自动发现需要摄像头支持 ONVIF。
- 目标机需要允许程序访问摄像头、串口和上传服务器。

## 开发运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:5000`。服务只允许本机访问。首次启动会创建 `config.json` 和 `runtime` 目录。

网页使用顺序：

1. 在“添加摄像头”中填写账号、密码并搜索，或手动填写 RTSP 地址。
2. 在“完整配置”中检查录像、OCR、串口、存储和上传参数。
3. 保存配置后确认“系统状态”中的摄像头和串口状态正常。
4. 在“事件与上传”中查看处理结果，并对失败任务执行手动重试。

## 配置与数据

配置文件为程序目录下的 `config.json`。主要配置段如下：

| 配置段 | 用途 |
| --- | --- |
| `http` | 本地 Web 服务地址和端口 |
| `recording` | 触发前后时长与切片时长 |
| `preview` | 预览宽度、帧率和空闲停止时间 |
| `ocr` | OCR 开关、抽样帧率、置信度和 CPU 线程数 |
| `upload` | 文本/视频端点、Token、超时和重试策略 |
| `storage` | 磁盘保留空间与缓存保留时间 |
| `cameras` | 摄像头身份和 RTSP 地址 |
| `serial_ports` | 串口设备身份、波特率和触发指令 |

运行数据目录：

- `runtime\cache`：循环录像切片。
- `runtime\events`：可恢复任务清单和事件视频。
- `runtime\logs`：应用与 OCR 日志。

修改配置前应备份 `config.json` 和 `runtime\events`。不要手工修改正在处理的事件清单。

## 串口协议

默认配置自动扫描设备 `trigger_1`，波特率为 9600：

```json
{"device_id":"trigger_1","baudrate":9600,"mode":"text","trigger":"XGFZJ:TRIGGER:trigger_1:1\r\n","enabled":true}
```

程序每 2 秒扫描当前串口，并使用以下 ASCII 协议确认设备身份：

```text
PC -> 设备: XGFZJ:DISCOVER:1\r\n
设备 -> PC: XGFZJ:DEVICE:<device_id>:1\r\n
设备 -> PC: XGFZJ:TRIGGER:<device_id>:1\r\n（触发录像）
```

设备应在 800 ms 内响应。绑定后还需响应每 5 秒一次的探测心跳；串口消失或心跳超时后，程序会释放原 COM 号并重新扫描。`device_id` 只允许 1–40 位英文字母、数字、下划线或连字符，且不得重复。

`mode` 可设为 `hex` 并使用既有硬件的固定字节指令，但设备身份探测协议保持不变。任一有效触发指令都会触发全部已启用摄像头。

## 上传协议

文本端点接收 JSON，视频端点接收 `multipart/form-data`，字段为 `metadata` 和 `file`。两类请求均包含 `Idempotency-Key`。

成功回执必须包含：

```json
{"accepted":true,"event_id":"...","camera_id":"..."}
```

视频回执还必须返回与上传文件一致的 `size` 和 `sha256`。只有文本、视频回执都通过校验，并且启用 `delete_after_success` 时，程序才删除本地视频。

## 准备依赖与发布

首次构建或更新第三方工具时执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fetch_dependencies.ps1
```

生成 release：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
```

发布目录为 `dist\XgfzjRecorder`，整体复制到目标机即可运行，不需要安装 Python。构建流程会执行：

- Python 单元测试。
- FFmpeg H.264/H.265 切片拼接验证。
- PyInstaller 打包。
- 打包 EXE 冒烟测试。

依赖脚本固定 OCR 上游 commit，生成 SHA-256 清单，并拒绝仍为 Git LFS 指针的必要文件。脚本还会验证并修补上游 worker 的默认 `OCR.yaml` 路径，结果写入 `tools\ocr\WORKER_PATCH.json`。

## 日志与排错

- `runtime\logs\app.log`：UTF-8 JSON Lines，单文件上限 5 MiB，保留 3 个备份。
- `runtime\logs\ocr.log`：OCR worker 的 stderr，达到 5 MiB 后轮换为 `ocr.log.1`。
- 网页状态页显示最近 20 条影响业务运行的错误；完整历史以日志文件为准。

排查顺序：先查看网页“系统状态”，再查看 `app.log` 和 `ocr.log`，最后分别确认 RTSP、串口、磁盘空间及上传端点连通性。日志会隐藏 RTSP 凭证，但仍应按敏感运行数据管理。

## 安全与限制

- `config.json` 可能包含 RTSP 凭证、服务器 URL 和 Token。网页接口会遮蔽敏感值，但文件本身应限制 Windows ACL，并禁止提交到 Git。
- ONVIF 扫描凭证只保存在当前进程内存中；扫描结果不会返回含凭证的 URL。
- 视频上传采用流式读取，并限制连接、无进展及总时长。
- 重叠事件可共享受引用计数保护的切片；编码参数不同的切片会拒绝拼接。
- 网页 MJPEG 预览需要解码和 JPEG 编码，同一时间只预览当前选中的一路。
- 1 FPS OCR 抽样无法保证识别只出现极短时间的车牌。
- 当前固定上游版本的 `ppocr_service.exe` 与 `ppocr_worker.exe` 参数协议不同，且不接受上游 README 所述的 `--fast_detect` 参数。本项目直接管理 worker 的 stdin/stdout 协议。
- 实机吞吐、预览延迟和车牌准确率必须使用目标硬件与真实样本验收。
