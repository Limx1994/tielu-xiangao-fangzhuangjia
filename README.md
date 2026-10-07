# 嵌入式 Windows 事件录像系统

面向 Windows 10/11 x64 工控机的本地事件录像程序。串口收到指定指令后，系统无损拼接触发前后的录像切片，抽帧调用本机 PaddleOCR worker 识别车牌，并可靠上传识别结果和视频。

默认参数为触发前 120 秒、触发后 20 秒、每段 10 秒。OCR 固定以 2 FPS 从触发点向前后检测。目标设备为 2 GB 内存的 Windows x64 工控机，最多接入 2 路摄像头；项目优先保证结构简单、资源有界和低运行损耗。

## 界面预览

实时预览与摄像头管理：

![实时预览与摄像头管理](%E9%A1%B5%E9%9D%A21.jpg)

系统状态、配置与事件上传：

![系统状态、配置与事件上传](%E9%A1%B5%E9%9D%A22.jpg)

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
3. 在“系统配置”中检查录像、OCR 和上传参数。
4. 保存配置后确认“系统状态”中的摄像头、串口和转发状态正常。
5. 在“事件与上传”中查看处理结果。录像或 OCR 尚在等待、处理中时不能重试上传；处理结束后，可点击“重试上传”重新提交未完成的上传任务。该操作不会重新执行失败的录像或 OCR。

需要验证整条处理流程时，可点击页面顶部的“立即触发”；它会为所有已启用摄像头创建一次测试事件，磁盘可用空间不足时会被拒绝。

取消绑定摄像头只会停止该摄像头的录像、预览和实时转发，不会删除已有事件与录像。

## 配置与数据

配置文件为程序目录下的 `config.json`。主要配置段如下：

| 配置段 | 用途 |
| --- | --- |
| `http` | 本地 Web 服务地址和端口 |
| `recording` | 触发前后录像时长 |
| `ocr` | OCR 开关、置信度和 CPU 线程数 |
| `upload` | 文本/视频端点、超时和重试策略，以及仅供高级用户配置的 Token |
| `cameras` | 摄像头身份、RTSP 地址和可选实时转发目标 |

网页为常用配置段提供分组表单，每组单独验证和保存。配置版本、网页监听地址和端口，以及 `upload.token` 属于高级配置，不在页面显示；默认值分别为 `1`、`127.0.0.1`、`5000` 和空字符串，高级用户可以直接编辑 `config.json`。摄像头可以编辑名称、启停状态、主码流 RTSP 地址和实时转发目标，预览固定使用主码流。最多配置 2 路摄像头，已有摄像头 ID 只读，手动新增摄像头时可以选填 ID。

管理页面同一时间只允许一个浏览器会话操作，业务请求会自动续租；页面停止请求 30 秒后，其他浏览器才能接管。健康和就绪检查不占用浏览器会话。实时预览保持主码流原始分辨率，固定为 5 FPS，并在无人查看 15 秒后停止解码；预览仍按需启动，每个活动预览使用 2 个 FFmpeg 线程。全系统同时只允许一个实时预览连接，现有连接关闭后才能接入。上述参数由程序内置，不提供页面或 JSON 配置项；旧配置中的 `preview` 段会在启动时自动移除。

磁盘保护阈值固定为 2 GiB。录像切片固定为 10 秒，缓存保留时间自动取“触发前录像时长 + 触发后录像时长 + 20 秒”，以覆盖完整事件窗口、两个切片的边界与清理余量。两项存储策略均由程序内置，不提供页面或 JSON 配置项；旧配置中的 `storage` 段会在启动时自动移除。

串口使用程序内置的 `trigger_1 / 9600 baud / text` 协议自动扫描、绑定和重连。网页只显示串口通讯状态、当前 COM 口、最近心跳、最近触发及三轴加速度，不提供串口配置入口。公共配置 API 不返回 `serial_ports`，保存其他配置时也会保留内置串口设置；旧配置中的自定义或空串口列表会在启动时自动恢复为内置设置。

网页保存配置后会立即应用运行时参数，并只重建配置发生变化的摄像头任务；`http.host` 和 `http.port` 会保存，但需要重启应用后生效。页面中的敏感地址留空表示保持现值，可使用对应的“清除”选项移除已保存值；Bearer Token 只能通过 `config.json` 的 `upload.token` 配置。直接编辑 `config.json` 时，应先退出应用，修改完成后重新启动；运行中的应用不会自动重新载入外部文件改动。

OCR 抽样帧率固定为 2 FPS，不提供配置项。旧版本 `config.json` 中的 `ocr.fps` 会在加载或保存配置时自动移除，其余扩展字段保持不变。

存在未完成事件时，系统会拒绝停用、删除该摄像头或修改其录像码流，防止事件处理中途失去数据源。应先等待事件完成或在“事件与上传”中处理失败任务。

事件上报地址和事件视频上传地址支持完整的 HTTP/HTTPS URL，也支持裸 IP、端口及路径，例如 `192.168.1.20:8080/report`；裸地址会自动补全为 `http://`。应用的 HTTP/ONVIF 会话不读取环境代理，FFmpeg、FFprobe 和 OCR 子进程也不继承代理变量。Proxifier 等 Winsock 透明代理必须额外将 `XgfzjRecorder.exe`、`python.exe`、`ffmpeg.exe` 和 `ffprobe.exe` 设置为 `Direct`。

每路摄像头可设置独立的 `forward_url`，支持 `192.168.2.20:5000`、裸 IP（默认端口 5000）或 `udp://192.168.2.20:5000`；显式端口必须在 1–65535 范围内，端口 0 会被拒绝。发送采用 MPEG-TS over UDP 单播，只复制视频码流、不转码且不发送音频；进程异常退出后会退避重连。跨网段时必须由网关提供路由，并在接收端防火墙放行对应 UDP 端口；两路摄像头不可使用同一个目标。接收端示例：

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

### 物理层和帧格式

默认设备 ID 为 `trigger_1`，串口参数如下：

- 波特率：`9600 baud`
- 数据位：`8 bit`
- 校验位：`None`
- 停止位：`1 bit`
- 流控：无
- 文本编码：`ASCII`
- 帧结束符：`\r\n`，即十六进制 `0D 0A`

所有指令都是一行 ASCII 文本。尖括号表示需要替换的字段，不是实际发送内容。程序内置设置为：

```json
{"device_id":"trigger_1","baudrate":9600,"mode":"text","trigger":"XGFZJ:TRIGGER:trigger_1:1\r\n","enabled":true}
```

### 1. 自动扫描和绑定

程序启动后枚举所有 COM 口。未绑定时，每轮扫描依次打开尚未占用的 COM 口并发送：

```text
PC -> 设备: XGFZJ:DISCOVER:1\r\n
```

加速度计必须在 800 ms 内返回自己的设备 ID：

```text
设备 -> PC: XGFZJ:DEVICE:trigger_1:1\r\n
```

各字段含义：

- `XGFZJ`：固定协议标识。
- `DEVICE`：设备身份回应。
- `trigger_1`：设备 ID，必须与 PC 内置设备 ID 完全一致。
- 最后的 `1`：协议版本号。

PC 收到完整且匹配的身份回应后，当前 COM 口进入 `bound` 状态。未响应或设备 ID 不匹配时关闭该 COM 口并继续检查下一个；一轮结束后固定等待 2 秒再重新扫描。绑定无需网页操作。

### 2. 绑定后的心跳

绑定成功后，PC 每 5 秒发送一次独立心跳包：

```text
PC -> 设备: XGFZJ:HEARTBEAT:1\r\n
```

加速度计必须在 1 秒内返回：

```text
设备 -> PC: XGFZJ:ALIVE:trigger_1:1\r\n
```

`ALIVE` 中的设备 ID 必须为当前绑定设备。设备端新实现必须返回完整、匹配的 `ALIVE` 帧；PC 为兼容旧固件也接受重复发送的匹配 `DEVICE` 身份帧，但不建议新固件依赖此兼容行为。出现以下任一情况时判定设备掉线：

- 发送或读取串口失败；
- 串口被拔出；
- 心跳发送后 1 秒内未收到匹配的 `ALIVE`。

掉线后程序关闭串口、释放原 COM 口，等待 2 秒，然后重新执行自动扫描和绑定。COM 号发生变化也不需要用户处理。

### 3. 三轴加速度触发

检测到事件时，加速度计主动发送：

```text
设备 -> PC: XGFZJ:TRIGGER:trigger_1:1:<x>:<y>:<z>\r\n
```

字段定义：

- `TRIGGER`：固定触发指令。
- `trigger_1`：当前设备 ID。
- `1`：协议版本号。
- `x`、`y`、`z`：触发瞬间三轴加速度，单位统一为 `g`。
- 三轴值必须是有限 ASCII 数值，可使用负号和小数点，例如 `-0.125`、`0`、`1.037`；禁止缺少任一轴，也禁止 `NaN` 和 `Inf`。

完整示例：

```text
设备 -> PC: XGFZJ:TRIGGER:trigger_1:1:0.125:-0.050:1.037\r\n
```

PC 收到后会立即触发所有已启用摄像头，并保存以下数据：

```json
{
  "source": "serial:trigger_1",
  "trigger": {
    "command": "XGFZJ:TRIGGER:trigger_1:1",
    "acceleration": {"x": 0.125, "y": -0.05, "z": 1.037}
  }
}
```

串口可能分多次收到一帧，也可能一次收到多帧；程序按 `\r\n` 组帧。与触发指令前缀无关的完整文本帧会被忽略；触发前缀匹配但三轴字段错误的帧会明确记录错误，不会触发录像。单个未结束帧超过 4096 字节时会被丢弃并上报。

### 4. 完整通讯时序示例

```text
PC     -> 设备  XGFZJ:DISCOVER:1\r\n
设备   -> PC    XGFZJ:DEVICE:trigger_1:1\r\n
PC     -> 设备  XGFZJ:HEARTBEAT:1\r\n
设备   -> PC    XGFZJ:ALIVE:trigger_1:1\r\n
设备   -> PC    XGFZJ:TRIGGER:trigger_1:1:0.125:-0.050:1.037\r\n
PC     -> 设备  XGFZJ:HEARTBEAT:1\r\n
设备   -> PC    XGFZJ:ALIVE:trigger_1:1\r\n
```

内置 `trigger` 表示触发指令前缀，不包含其后的三轴数值。任一有效触发指令都会触发全部已启用摄像头。

## 上传协议

事件端点仅在触发事件后接收 JSON，内容包含事件 ID、摄像头 ID、触发时间、触发指令、三轴加速度、录像覆盖范围和 OCR 结果。视频端点接收 `multipart/form-data`，字段为 `metadata` 和 `file`。两类请求均包含 `Idempotency-Key`，各自独立重试；未配置视频端点或没有视频文件时，不阻塞事件文本上报。

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

依赖脚本固定 OCR 上游 commit，并固定下载 FFmpeg 8.1.1 essentials build。OCR fallback 会用 LFS pointer 中的 SHA-256 校验 LFS 对象、用 Git blob SHA-1 校验普通文件；FFmpeg 压缩包使用固定 SHA-256 校验。脚本拒绝仍为 Git LFS 指针的必要文件，并验证、修补上游 worker 的默认 `OCR.yaml` 路径，结果写入 `tools\ocr\WORKER_PATCH.json`。

release 成功标准：构建命令退出码为 0，Ruff 和单元测试通过，`dist\XgfzjRecorder\XgfzjRecorder.exe` 可通过就绪、首页和单实例检查，FFmpeg H.264/H.265 验证通过，且 OCR 发布清单中的文件数量和 SHA-256 均一致。任一检查失败都不得交付新目录。

## 日志与排错

`/api/health` 用于确认 HTTP 服务存活；`/api/ready` 检查工具、模型、OCR 配置是否存在以及程序目录是否可写，不代表已验证摄像头连接、OCR 识别或上传服务器。

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
- OCR 在事件视频拼接后离线执行，以 10 秒为一个有界批次，固定按 2 FPS 抽取配置的全部触发前录像及触发后至视频结束的画面。每批按距触发点由近到远检测，等距时优先触发后的帧；OCR worker 复用单个响应读取线程和单个临时帧文件。首次识别到合格车牌即停止，连续 3 帧处理失败时终止当前事件 OCR，避免故障期间重复启动 worker 和刷写日志。默认 120 秒触发前、20 秒触发后且始终无合格车牌时，最多约执行 280 次 OCR；增加录像时长会相应增加最坏耗时。
- 当前固定上游版本的 `ppocr_service.exe` 与 `ppocr_worker.exe` 参数协议不同，且不接受上游 README 所述的 `--fast_detect` 参数。本项目直接管理 worker 的 stdin/stdout 协议。
- 2 GB 是目标运行约束，不代表仅凭静态检查已经验证双路全功能峰值；实机内存峰值、吞吐、预览延迟、长时间稳定性和车牌准确率必须使用目标硬件与真实样本验收。

## 清理开发残留

先停止服务、测试和构建，再预览清理范围：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\clean_project.ps1 -WhatIf
```

确认后去掉 `-WhatIf` 执行。脚本将指定缓存、测试残留和指定空目录移到 Windows 回收站，保留配置、日志、录像、发布目录、工具及虚拟环境；移动到回收站不会释放磁盘空间。

## 许可证

本项目使用 [LICENSE](LICENSE) 中的 PolyForm Noncommercial License 1.0.0 及附加限制条款。第三方工具、模型和运行库适用各自上游许可证，OCR 来源说明见 [运行时文档](tools/ocr/README.md)。交付发布目录时应同时附带根目录 `LICENSE`。
