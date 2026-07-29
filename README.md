# CC Switch Batch Sender

一个面向 Windows 的轻量批量请求工具。应用只读本机 CC Switch 的 Codex provider，默认跟随 CC Switch 当前上游，也可以在本次运行中选择其他 provider。

## 界面能力

- 默认选中 CC Switch 当前 Codex provider；刷新后重新读取当前指针和 provider 列表。
- 下拉切换只影响本工具本次发送，不写回 CC Switch，不修改 `cc-switch.db`。
- 可编辑提示词、每批请求次数、额外批次重试次数、重试间隔、模型/地址覆盖、超时、Endpoint 模式和输出 token。
- 自动展示实际 JSON 请求体，也可切换为自定义 JSON 后直接编辑。
- 展示发送上限、批次进度、完成/失败数、首个成功响应、耗时和完整运行日志。
- 默认不生成 `config.json`、日志文件、结果文件或锁文件。结果和日志只在应用内保留，需要时由用户显式导出。
- 默认设置保存在当前用户注册表 `HKCU\Software\CCSwitchBatchSender`，不保存 API Key。
- 使用 Windows 命名互斥体防止重复启动，不产生 `run.lock`。

## 重试语义

“请求次数”是每批并发 POST 数；“重试次数”是首批全部失败后额外发送的批次数，不是对每个请求再次重试。

最大客户端 POST 数：

```text
请求次数 × (1 + 重试次数)
```

默认请求次数为 20，重试次数为 0，所以默认上限仍是 20 个 POST。任意请求成功后不会再开启下一批；已经发送的同批请求会继续收尾，以保留原工具面向 Sub2API 池内重试/换号的行为。

## 运行

正式交付直接双击：

```text
dist\CCSwitchBatchSender.exe
```

也可以双击 `run_ccswitch_batch_sender.bat`。脚本会优先启动 EXE；没有 EXE 时回退到本机 Python。

源码运行：

```powershell
python .\ccswitch_batch_sender.py --gui
```

只读检查当前 provider，不发送请求：

```powershell
python .\ccswitch_batch_sender.py --dry-run
```

无界面发送，并覆盖请求次数、额外重试次数和提示词：

```powershell
python .\ccswitch_batch_sender.py --headless --count 20 --retry-count 2 --message "1"
```

成功后显式导出结果：

```powershell
python .\ccswitch_batch_sender.py --headless --output .\result.json
```

`--config path.json` 仅作为临时高级入口保留，默认运行不依赖任何配置文件。

## 构建单文件 EXE

环境要求：Python 3.10+、Tk 8.6、PyInstaller 6.14+、Pillow（仅用于生成图标，不打入应用）。构建脚本会自动选择本机已具备这些模块的 Python。

缺少构建依赖时可先执行：

```powershell
python -m pip install -r .\requirements-build.txt
```

```powershell
.\build_single_exe.ps1
```

构建会先生成 Lucide 风格图标并运行离线测试，再产出：

```text
dist\CCSwitchBatchSender.exe
```

发布目录只需要这个 EXE。应用不使用 UPX，减少杀软误报；首次启动的 onefile 解压耗时属于 PyInstaller 正常行为。

## 安全边界

- 以 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 只读打开 `%USERPROFILE%\.cc-switch\cc-switch.db`。
- 当前 provider 优先读取 `%USERPROFILE%\.cc-switch\settings.json` 的 `currentProviderCodex`，无有效指针时才回退到数据库唯一的 `is_current=1`。
- 不显示、不复制、不记录 API Key；注册表设置也不包含 Key。
- provider 下拉只选择本工具的请求来源，不执行 CC Switch 的正式切换操作。
- 自定义请求体和完整响应导出由用户显式启用；默认结果只包含脱敏 Endpoint、状态、耗时、文本和 usage。
