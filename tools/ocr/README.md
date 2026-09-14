# 本项目的 PaddleOCR 运行时

本目录保存 `XgfzjRecorder` 使用的 Windows PaddleOCR 二进制、模型、运行库和来源校验信息。它由 `scripts\fetch_dependencies.ps1` 按固定上游 commit 生成，不是本项目的独立源码模块，也不应手工拼装或局部升级。

## 项目集成方式

应用直接启动 `ppocr_worker.exe`，通过 stdin/stdout 的逐行协议复用常驻进程：

1. worker 启动完成后输出 `READY`。
2. 应用向 stdin 写入一行待识别图片的绝对路径。
3. worker 返回 `OK <json>` 或 `ERR <message>`。

应用启动 worker 时使用以下参数：

```powershell
.\ppocr_worker.exe --model_dir .\models --cpu_threads 2 --use_doc_orientation false
```

正常使用时无需手工启动 OCR 进程；运行 `app.py` 或打包后的 `XgfzjRecorder.exe` 即可。

事件视频拼接完成后，应用固定以 2 FPS 抽帧。扫描以触发点为中心，每次读取前后各最多 5 秒画面，并按距触发点由近到远送入 worker；首次识别到满足格式和置信度要求的车牌后停止。该帧率不是 worker 参数，也不能通过 `config.json` 调整；旧版 `ocr.fps` 字段会由应用自动移除。

## 目录内容

```text
tools\ocr\
├── ppocr_worker.exe              # 应用实际调用的常驻 OCR worker
├── models\                       # 文本检测、识别、方向分类和车牌模型
├── configs\OCR.yaml             # worker 配置
├── *.dll                         # Paddle、OpenCV、ONNX Runtime、MinGW 运行库
├── UPSTREAM_COMMIT.txt           # 固定的上游 commit
├── WORKER_PATCH.json             # worker 路径补丁及哈希
└── SHA256SUMS.json               # 当前目录文件校验清单
```

模型用途：

| 模型 | 用途 |
| --- | --- |
| `PP-OCRv4_mobile_det_infer` | 文本区域检测 |
| `PP-OCRv4_mobile_rec_infer` | 文本识别 |
| `PP-LCNet_x1_0_doc_ori_infer` | 文档方向分类；本项目默认关闭 |
| `plate_rtdetr.onnx` | 上游附带的车牌检测模型 |

方向分类模型的原始模型卡位于 `models\PP-LCNet_x1_0_doc_ori_infer\README.md`。

## 获取或更新依赖

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fetch_dependencies.ps1
```

该命令同时恢复 OCR 和 FFmpeg 依赖；本节仅说明 OCR 目录的产物。FFmpeg 固定为 8.1.1 essentials build，下载压缩包会先校验 SHA-256，再提取 `ffmpeg.exe` 和 `ffprobe.exe`。

OCR 获取流程会：

- 获取指定 OCR 上游 commit，而不是不受控地使用最新版本。
- 下载项目所需的 Git LFS 文件并拒绝残留的指针文件；不下载未使用的 TCP service。
- 复制本项目的 `ocr\OCR.yaml`。
- 校验并修补 worker 默认配置路径。
- 清理上游残留的无名 `.json` 文件。
- 生成 `UPSTREAM_COMMIT.txt`、`WORKER_PATCH.json` 和 `SHA256SUMS.json`。

当前固定 commit 以 `UPSTREAM_COMMIT.txt` 为准。不要直接替换单个 EXE、DLL 或模型，否则容易造成 ABI、运行库或参数协议不匹配。

依赖更新成功的判定条件是：必要文件均不是 Git LFS 指针、worker 默认配置路径已经校正、校验清单已重新生成，并且项目 release 构建和 OCR 启动检查通过。

## Release 打包行为

`scripts\build.ps1` 只将应用需要的 OCR 文件复制到 staging release，不包含未使用的 `ppocr_service.exe`、上游残留的无名 `.json` 文件，也不会直接复用源码目录中的 `SHA256SUMS.json`。脚本会基于实际发布内容重新生成校验清单，并逐项复算 SHA-256；校验或冒烟测试失败时不会替换已有 release。

因此，源码依赖目录与发布目录的校验清单文件数量可能不同，这是裁剪未使用组件后的预期结果。排查发布包时应以 `dist\XgfzjRecorder\tools\ocr\SHA256SUMS.json` 为准，不应将源码清单直接覆盖到 release。

## 手工诊断

仅在排错时直接启动 worker：

```powershell
Set-Location tools\ocr
.\ppocr_worker.exe --model_dir .\models --cpu_threads 2 --use_doc_orientation false
```

看到 `READY` 后输入一张本地图片的绝对路径。应用侧日志位于：

```text
runtime\logs\ocr.log
runtime\logs\ocr.log.1
```

常见问题：

| 现象 | 检查项 |
| --- | --- |
| worker 启动后立即退出 | `models`、`configs\OCR.yaml`、MinGW DLL 是否齐全 |
| 报 OCR 文件缺失或 LFS 指针 | 重新运行依赖脚本，确认下载完整 |
| 30 秒内没有 `READY` | 查看 `runtime\logs\ocr.log`，检查模型加载和内存 |
| 识别结果为空 | 检查图片质量、车牌出现时长、OCR 置信度，以及事件视频是否覆盖触发前后画面 |
| DLL 加载失败 | 不要混用其他 MinGW 或 PaddleOCR 发行包中的 DLL |

## 已知边界

- 本项目不下载或打包 `ppocr_service.exe`，不要将 service 参数套用于 worker。
- 当前固定版本不接受上游旧说明中的 `--fast_detect` 参数。
- OCR 输出还会经过应用的车牌格式和置信度过滤；worker 有文字输出不代表最终一定生成车牌结果。
- 应用启动 OCR 子进程时会移除代理环境变量；透明代理软件仍需将应用、Python 和 OCR 进程设置为直连。
- 运行时体积较大，Git 只保存项目文档和获取脚本，二进制及模型由依赖脚本恢复。

## 来源与许可证

- 上游项目：<https://github.com/Limx1994/PaddleOCR-MinGW-LMX>
- PaddleOCR：<https://github.com/PaddlePaddle/PaddleOCR>
- 各模型和二进制的许可证以对应上游文件为准；方向分类模型卡声明 Apache License 2.0。
