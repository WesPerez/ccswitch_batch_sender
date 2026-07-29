# CC Switch Batch Sender

一个面向 Windows 的轻量批量请求工具。应用只读本机 CC Switch 的 Codex provider，默认跟随 CC Switch 当前上游，也可以在本次运行中选择其他 provider。

## 界面能力

- 默认选中 CC Switch 当前 Codex provider；刷新后重新读取当前指针和 provider 列表。
- 下拉切换只影响本工具本次发送，不写回 CC Switch，不修改 `cc-switch.db`。
- 默认由本机官方 Codex CLI 为每个任务发起请求；不伪造 Codex 请求头或客户端元数据。
- 可切换到“直接 API”模式，以保留完整自定义 JSON、精确 POST 数和原始响应控制。
- 默认让每个任务使用独立的随机内容，也可关闭后改用固定提示词。
- 可编辑固定提示词、每批请求次数、额外批次重试次数、重试间隔、模型/地址覆盖、超时、Endpoint 模式和输出 token。
- 官方 CLI 模式展示任务摘要，实际请求头和请求体由 Codex CLI 生成；直接 API 模式展示实际 JSON，并可切换为自定义 JSON。
- 展示发送上限、批次进度、完成/失败数、首个成功响应、耗时和完整运行日志。
- 默认不生成 `config.json`、日志文件、结果文件或锁文件。结果和日志只在应用内保留，需要时由用户显式导出。
- 默认设置保存在当前用户注册表 `HKCU\Software\CCSwitchBatchSender`，不保存 API Key。
- 使用 Windows 命名互斥体防止重复启动，不产生 `run.lock`。

## 请求来源与重试语义

### 官方 Codex CLI（默认）

- “请求次数”是每批 Codex 任务数；每个任务由已安装的官方 `codex exec` 独立发起。
- “CLI 并发”限制同时运行的 Codex 进程数，默认 4，避免一次启动 20 个进程占用过多内存。
- “重试次数”是整批没有成功结果后额外启动的任务批次数。
- 一次 Codex 任务内部实际产生多少 HTTP 请求、流式重连或官方重试由 Codex CLI 决定，因此界面不把任务数描述成精确 POST 数。
- 停止任务或关闭应用时，只终止本应用启动并登记的 Codex CLI 进程树，不影响 Codex Desktop 或其他 Codex 任务。
- 仅支持 Responses API provider。Chat Completions provider 需使用直接 API 模式。

应用通过单次子进程环境变量向 `codex exec` 提供所选 provider 的 API Key，并通过临时 `-c` 参数覆盖 model 和 base URL。Key 不进入命令行、日志、注册表或配置文件。

### 直接 API

“请求次数”是每批并发 POST 数；“重试次数”是首批全部失败后额外发送的批次数，不是对每个请求再次重试。

最大客户端 POST 数：

```text
请求次数 × (1 + 重试次数)
```

默认请求次数为 20，重试次数为 0。直接 API 模式的默认上限是 20 个 POST；官方 CLI 模式的默认上限是 20 个逻辑任务。任意任务成功后不会再开启下一批，已经启动的同批任务会继续收尾。

## 请求体与随机任务

- 官方 CLI 模式只把用户任务交给 `codex exec`，完整 Responses 请求由官方客户端构造。
- 直接 API 的自动 Responses 请求只把任务放入 `input`，不再额外生成 `instructions: "Return ..."`。
- 随机任务来自小型自然任务池，覆盖计算、排序、单位换算、文本转换和结构化输出；只有 HTTP 成功且返回 JSON 通过语义校验时才计为成功。
- 默认输出上限为 64 token。关闭“每次随机任务”后，应用使用左侧编辑的固定提示词。
- 直接 API 自动请求中的 `<每个请求唯一>` 和 `<每个请求随机任务>` 是预览占位符，每个 POST 发送前分别替换为 UUID 和本次真实任务；旧的 `<每个请求随机探针>` 仍兼容。
- 自定义 JSON 也识别这两个精确占位符；固定的 `prompt_cache_key` 会原样保留，模板内容不会被运行时替换污染。
- 自动生成模式下请求体整体只读；自定义模式下，包含自动替换占位符的 JSON 行会高亮，并在工具栏标明发送时替换。

直接 API 模式可读取本机 `codex --version` 对应的已安装 CLI 版本，并添加自定义请求头：

```text
X-CCSwitch-Local-Codex-CLI-Version: <当前版本>
```

`X-CCSwitch-` 是本应用的请求头命名空间，并不表示这是 CC Switch 官方协议。该请求头只存在于直接 API 模式，用户可在高级设置关闭。

官方 CLI 模式不发送本应用的 `User-Agent`、`Originator` 或 `X-CCSwitch-*` 头；客户端身份和请求元数据完全由本机官方 Codex CLI 生成。直接 API 模式会明确使用 `CC Switch Batch Sender/... (non-codex)` 身份，不冒充官方 Codex。

运行日志只保存在本机应用内，不会随请求发送。所选 provider 能看到实际 HTTP 请求头和请求体；后续中继是否继续转发这些字段取决于中继实现。

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
python .\ccswitch_batch_sender.py --headless --count 20 --retry-count 2 --fixed-prompt --message "请概括客户端超时的作用。"
```

使用默认随机任务：

```powershell
python .\ccswitch_batch_sender.py --headless --random-tasks
```

显式选择请求来源：

```powershell
python .\ccswitch_batch_sender.py --headless --transport codex_cli --cli-concurrency 4
python .\ccswitch_batch_sender.py --headless --transport direct
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
- provider 或 CLI 返回内容进入日志和结果前，会再次按当前 API Key 做精确脱敏。
- provider 下拉只选择本工具的请求来源，不执行 CC Switch 的正式切换操作。
- 官方 CLI 模式由当前安装的 `codex exec` 发起请求并保留其自动元数据，不伪造任何官方字段。
- 直接 API 模式仅用于通用传输和自定义 JSON，始终明确标记为非 Codex 客户端。
- 自定义请求体和完整响应导出由用户显式启用；默认结果只包含脱敏 Endpoint、状态、耗时、文本和 usage。
