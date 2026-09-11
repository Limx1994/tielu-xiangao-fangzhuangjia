# 本项目的 PaddleOCR 运行时

本目录保存 `XgfzjRecorder` 使用的 Windows PaddleOCR 二进制、模型、运行库和来源校验信息。它是由 `scripts\fetch_dependencies.ps1` 获取的第三方运行时，不是本项目的独立源码模块。

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

## 目录内容

```text
tools\ocr\
├── ppocr_worker.exe              # 应用实际调用的常驻 OCR worker
├── ppocr_service.exe             # 上游附带的 TCP service，本项目不调用
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

脚本会：

- 获取指定 OCR 上游 commit，而不是不受控地使用最新版本。
- 下载 Git LFS 文件并拒绝残留的指针文件。
- 复制本项目的 `ocr\OCR.yaml`。
- 校验并修补 worker 默认配置路径。
- 生成 `UPSTREAM_COMMIT.txt`、`WORKER_PATCH.json` 和 `SHA256SUMS.json`。

当前固定 commit 以 `UPSTREAM_COMMIT.txt` 为准。不要直接替换单个 EXE、DLL 或模型，否则容易造成 ABI、运行库或参数协议不匹配。

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
| 识别结果为空 | 检查图片质量、车牌出现时长、OCR 置信度和抽样 FPS |
| DLL 加载失败 | 不要混用其他 MinGW 或 PaddleOCR 发行包中的 DLL |

## 已知边界

- 本项目不调用 `ppocr_service.exe`，不要将 service 参数套用于 worker。
- 当前固定版本不接受上游旧说明中的 `--fast_detect` 参数。
- OCR 输出还会经过应用的车牌格式和置信度过滤；worker 有文字输出不代表最终一定生成车牌结果。
- 运行时体积较大，Git 只保存项目文档和获取脚本，二进制及模型由依赖脚本恢复。

## 来源与许可证

- 上游项目：<https://github.com/Limx1994/PaddleOCR-MinGW-LMX>
- PaddleOCR：<https://github.com/PaddlePaddle/PaddleOCR>
- 各模型和二进制的许可证以对应上游文件为准；方向分类模型卡声明 Apache License 2.0。
