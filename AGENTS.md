# 仓库指南

## 项目结构与模块组织

本项目是面向 Windows 的 Python 事件录像服务。`app.py` 负责 Flask 管理界面、录像调度、串口处理、OCR 流程及上传任务。根目录中的 `manifest_store.py` 管理事件索引，`network_utils.py` 处理网络配置，`stream_forwarder.py` 负责 UDP 转发。前端资源位于 `web/`，OCR 默认配置位于 `ocr/`，测试位于 `tests/`，依赖下载与发布脚本位于 `scripts/`。`runtime/`、`build/`、`dist/`、`config.json` 及 `tools/` 下下载的工具均为生成内容或本机数据，不应提交。本项目最多链接2路摄像头，不需要考虑高并发，性能优先，结构简单优先。

## 低配置运行约束

- 目标运行环境为 Windows 10/11 x64、2 GB RAM，最多连接 2 路摄像头；所有设计和修改必须优先控制 CPU、内存、磁盘 I/O、进程数和线程数。
- 录像、拼接和转发必须优先使用 FFmpeg Stream Copy，禁止无必要的视频重编码；预览必须按需启动并限制解码线程。
- 队列、帧缓冲、日志和重试任务必须有明确上限；禁止将完整事件视频读入内存，文件哈希和上传必须流式处理。
- OCR 保持单 worker 串行处理，默认最多使用 2 个 CPU 线程；不得为缩短单次耗时而引入无界并发。
- 新增依赖、常驻进程、后台线程或轮询任务前必须评估资源成本。无法确认 2 GB 环境稳定时，必须明确报告并通过目标工控机实测后才能宣称达标。

## 构建、测试与开发命令

在 Windows 10/11 x64 的 PowerShell 中执行：

```powershell
python -m pip install -r requirements.txt
python app.py
python -m ruff check --no-cache app.py manifest_store.py network_utils.py stream_forwarder.py tests
python -m pytest -q
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fetch_dependencies.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build.ps1
```

`python app.py` 启动本地管理界面，默认地址为 `http://127.0.0.1:5000`。首次构建或更新固定版本的 OCR、FFmpeg 资源时，先运行依赖下载脚本。构建脚本会执行 Ruff、单元测试、H.264/H.265 验证、PyInstaller 打包及 `dist\XgfzjRecorder` 冒烟测试。

## 编码风格与命名约定

Python 使用四空格缩进；函数和变量采用 `snake_case`，常量采用 `UPPER_CASE`。优先复用现有辅助函数和配置常量，保持模块职责集中。错误必须明确抛出、返回或上报，禁止用默认值隐藏失败。修改 JavaScript 和 CSS 时遵循 `web/app.js` 与 `web/style.css` 的现有风格。项目未强制配置格式化工具，因此应保持最小 diff，避免无关重排或重构。

## 测试规范

项目使用 Ruff 和 pytest。测试文件命名为 `test_*.py`，测试函数命名为 `test_*`。行为缺陷的回归测试优先添加到 `tests/test_app.py` 中相邻功能区域；打包程序和 FFmpeg 集成检查使用独立脚本。每次提交前依次运行 Ruff 和 `python -m pytest -q`。涉及发布流程的修改必须执行完整构建脚本，并确认所有检查均以退出码 0 完成。

## 提交与 Pull Request 规范

Git 历史采用 Conventional Commits，常见前缀包括 `feat:`、`fix:` 和 `docs:`，例如 `fix: reject duplicate camera targets`。每个提交只包含一个逻辑变更。Pull Request 应说明问题、解决方案、验证命令以及配置或发布影响，并关联相关 issue。修改 Web UI 时附上截图。

## 安全与配置

禁止提交 `config.json`、运行日志、摄像头凭证、上传 Token 或事件视频。敏感值应保存在本地配置或环境变量中。分享日志和截图前必须移除凭证、Token、内网地址及其他敏感信息。
