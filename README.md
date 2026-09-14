# 嵌入式 Windows 事件录像系统

面向 Windows 10/11 x64 工控机的本地事件录像程序。串口收到指定指令后，系统无损拼接触发前后的录像切片，抽帧调用本机 PaddleOCR worker 识别车牌，并可靠上传识别结果和视频。

默认参数为触发前 120 秒、触发后 20 秒、每段 10 秒。OCR 固定以 2 FPS 从触发点向前后检测。目标设备为 2 GB 内存的 Windows x64 工控机，最多接入 2 路摄像头；项目优先保证结构简单、资源有界和低运行损耗。

## 主要功能

- 使用 FFmpeg Stream Copy 循环录像并拼接事件视频，避免重复编码。
- 通过 ONVIF 搜索摄像头，也支持手动填写 RTSP 地址。
- 自动识别串口设备身份，COM 号变化后自动重新绑定，并在状态页显示最近一次探测失败原因。
- 使用常驻 `ppocr_worker.exe` 完成本地 OCR；从触发点向前后分批检测，并按中国大陆车牌格式过滤结果。
- 文本与视频独立重试，使用幂等键和严格回执校验防止误删本地文件。
- 使用持久化清单索引恢复未完成任务，仅缓存最近 200 条历史事件，避免事件累积后反复全量扫描。
- 提供本机网页管理、实时预览、摄像头解绑、事件查询、手动重试和开机自启控制。
- 将摄像头主码流以 MPEG-TS over UDP 单播实时转发，接收端可直接使用 VLC 或 ffplay。
- 保存配置后立即重建受影响的录像、预览和转发任务；HTTP 监听地址变更除外。
- 磁盘空间不足时暂停新事件和录像缓存，空间恢复后自动重启录像。

## 运行要求

- Windows 10/11 x64。
- 目标内存为 2 GB；录像、拼接和转发采用 Stream Copy，OCR 默认单 worker、2 个 CPU 线程，预览按需启动。
- 开发运行需要 Python 及 `requirements.txt` 中的依赖；当前验证环境为 Python 3.14.6。
- 摄像头应提供可访问的 RTSP 主码流；自动发现需要摄像头支持 ONVIF。
- 目标机需要允许程序访问摄像头、串口和上传服务器。

## 开发运行

```powershell
python -m pip install -r requirements.txt
python app.py
```

打开 `http://127.0.0.1:5000`。服务只允许本机访问。首次启动会创建 `config.json` 和 `runtime` 目录。

提交前运行质量检查：

```powershell
python -m ruff check --no-cache app.py manifest_store.py network_utils.py stream_forwarder.py tests
python -m pytest -q
```

网页使用顺序：

1. 在“添加摄像头”中填写账号、密码并搜索，或手动填写 RTSP 地址。
2. 如需实时转发，为每路摄像头填写不同的接收端 IP 和 UDP 端口。
3. 在“系统配置”和“串口触发配置”中检查录像、预览、OCR、串口、存储和上传参数。
4. 保存配置后确认“系统状态”中的摄像头、串口和转发状态正常。
5. 在“事件与上传”中查看处理结果，并对失败任务执行手动重试。

取消绑定摄像头只会停止该摄像头的录像、预览和实时转发，不会删除已有事件与录像。

## 配置与数据

配置文件为程序目录下的 `config.json`。主要配置段如下：

| 配置段 | 用途 |
| --- | --- |
| `http` | 本地 Web 服务地址和端口 |
| `recording` | 触发前后时长与切片时长 |
| `preview` | 预览宽度、帧率和空闲停止时间 |
| `ocr` | OCR 开关、置信度和 CPU 线程数 |
| `upload` | 文本/视频端点、Token、超时和重试策略 |
| `storage` | 磁盘保留空间与缓存保留时间 |
| `cameras` | 摄像头身份、RTSP 地址和可选实时转发目标 |
| `serial_ports` | 串口设备身份、波特率和触发指令 |

网页为上述配置段提供分组表单，每组单独验证和保存。摄像头可以编辑名称、启停状态、主/预览 RTSP 地址和实时转发目标；串口设备可以新增、编辑、启停和删除。最多配置 2 路摄像头和 16 个串口设备。配置格式版本和已有摄像头 ID 只读，手动新增摄像头时可以选填 ID。

网页保存配置后会立即应用运行时参数，并只重建配置发生变化的摄像头任务；`http.host` 和 `http.port` 会保存，但需要重启应用后生效。敏感配置留空表示保持现值，可使用对应的“清除”选项移除已保存值。直接编辑 `config.json` 时，应先退出应用，修改完成后重新启动；运行中的应用不会自动重新载入外部文件改动。

OCR 抽样帧率固定为 2 FPS，不提供配置项。旧版本 `config.json` 中的 `ocr.fps` 会在加载或保存配置时自动移除，其余扩展字段保持不变。

存在未完成事件时，系统会拒绝停用、删除该摄像头或修改其录像码流，防止事件处理中途失去数据源。应先等待事件完成或在“事件与上传”中处理失败任务。

事件上报地址和事件视频上传地址支持完整的 HTTP/HTTPS URL，也支持裸 IP、端口及路径，例如 `192.168.1.20:8080/report`；裸地址会自动补全为 `http://`。应用的 HTTP/ONVIF 会话不读取环境代理，FFmpeg、FFprobe 和 OCR 子进程也不继承代理变量。Proxifier 等 Winsock 透明代理必须额外将 `XgfzjRecorder.exe`、`python.exe`、`ffmpeg.exe` 和 `ffprobe.exe` 设置为 `Direct`。

每路摄像头可设置独立的 `forward_url`，支持 `192.168.2.20:5000`、裸 IP（默认端口 5000）或 `udp://192.168.2.20:5000`。发送采用 MPEG-TS over UDP 单播，只复制视频码流、不转码且不发送音频；进程异常退出后会退避重连。跨网段时必须由网关提供路由，并在接收端防火墙放行对应 UDP 端口；两路摄像头不可使用同一个目标。接收端示例：

```powershell
ffplay -fflags nobuffer -flags low_delay udp://@:5000
```

VLC 可通过“媒体 → 打开网络串流”使用 `udp://@:5000` 接收。

运行数据目录：

- `runtime\cache`：循环录像切片。
- `runtime\events`：可恢复任务清单和事件视频。
- `runtime\events\.manifest-index.json`：活动任务与最近事件索引；缺失、损坏或与清单不一致时自动重建。
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

事件端点仅在触发事件后接收 JSON，内容包含事件 ID、摄像头 ID、触发时间、录像覆盖范围和 OCR 结果。视频端点接收 `multipart/form-data`，字段为 `metadata` 和 `file`。两类请求均包含 `Idempotency-Key`，各自独立重试；未配置视频端点或没有视频文件时，不阻塞事件文本上报。

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

- Ruff 静态检查。
- Python 单元测试。
- FFmpeg H.264/H.265 切片拼接验证。
- PyInstaller 打包。
- 打包 EXE 健康检查、就绪检查、首页访问和单实例冒烟测试。

构建先在 `dist` 下的临时 staging 目录完成打包和验证，通过后再原子替换 `dist\XgfzjRecorder`；切换失败会恢复旧 release。构建会复制根目录 `README.md` 和所需工具运行时，从 OCR 发布内容中移除未使用的 `ppocr_service.exe`，并针对实际发布文件重新生成和逐项验证 `SHA256SUMS.json`。若旧发布目录已有 `config.json`，脚本会保留该配置；构建完成后仍应检查发布目录中没有测试日志和不应交付的敏感配置。

依赖脚本固定 OCR 上游 commit，并固定下载 FFmpeg 8.1.1 essentials build；两者都会验证 SHA-256。脚本拒绝仍为 Git LFS 指针的必要文件，并验证、修补上游 worker 的默认 `OCR.yaml` 路径，结果写入 `tools\ocr\WORKER_PATCH.json`。

release 成功标准：构建命令退出码为 0，Ruff 和单元测试通过，`dist\XgfzjRecorder\XgfzjRecorder.exe` 可通过就绪、首页和单实例检查，FFmpeg H.264/H.265 验证通过，且 OCR 发布清单中的文件数量和 SHA-256 均一致。任一检查失败都不得交付新目录。

## 日志与排错

- `runtime\logs\app.log`：UTF-8 JSON Lines，单文件上限 5 MiB，保留 3 个备份。
- `runtime\logs\ocr.log`：OCR worker 的 stderr，达到 5 MiB 后轮换为 `ocr.log.1`。
- 网页状态页显示最近 20 条影响业务运行的错误；完整历史以日志文件为准。

排查顺序：先查看网页“系统状态”，再查看 `app.log` 和 `ocr.log`，最后分别确认 RTSP、串口、磁盘空间及上传端点连通性。日志会隐藏 RTSP 凭证，但仍应按敏感运行数据管理。

## 安全与限制

- `config.json` 可能包含 RTSP 凭证、服务器 URL 和 Token。网页摄像头配置会显示完整 RTSP 地址，上传服务器 URL 和 Token 不回显；配置文件仍应限制 Windows ACL，并禁止提交到 Git。
- ONVIF 扫描凭证只保存在当前进程内存中；扫描结果不会返回含凭证的 URL。
- 视频上传采用流式读取，并限制连接、无进展及总时长。
- 重叠事件可共享受引用计数保护的切片；编码参数不同的切片会拒绝拼接。
- 网页 MJPEG 预览需要解码和 JPEG 编码，同一时间只预览当前选中的一路。
- 预览和 OCR 抽帧的单帧缓冲上限为 25 MB；异常码流超过上限时会终止对应 FFmpeg 进程并记录明确错误。
- OCR 在事件视频拼接后离线执行，以 5 秒为一个有界批次，固定按 2 FPS 抽取配置的全部触发前录像及触发后至视频结束的画面。每批按距触发点由近到远检测，等距时优先触发后的帧；首次识别到合格车牌即停止。默认 120 秒触发前、20 秒触发后且始终无合格车牌时，最多约执行 280 次 OCR；增加录像时长会相应增加最坏耗时。
- 当前固定上游版本的 `ppocr_service.exe` 与 `ppocr_worker.exe` 参数协议不同，且不接受上游 README 所述的 `--fast_detect` 参数。本项目直接管理 worker 的 stdin/stdout 协议。
- 2 GB 是目标运行约束，不代表仅凭静态检查已经验证双路全功能峰值；实机内存峰值、吞吐、预览延迟、长时间稳定性和车牌准确率必须使用目标硬件与真实样本验收。
