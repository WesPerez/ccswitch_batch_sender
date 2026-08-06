# CC Switch Batch Sender

一个面向 Windows 的轻量批量请求工具。应用只读本机 CC Switch 的 Codex provider，默认跟随 CC Switch 当前上游，也可以在本次运行中选择其他 provider。

## 界面能力

- 默认选中 CC Switch 当前 Codex provider；刷新和开始运行时重新读取当前指针和 provider 列表。
- 下拉标签、预览和启动校验使用同一 provider 快照；一次运行（包括额外重试批次）固定使用启动时选定的 provider，不会因 CC Switch 中途切换而漂移。
- 下拉切换只影响本工具本次发送，不写回 CC Switch，不修改 `cc-switch.db`。
- 默认使用“直接 API”模式，以保留完整自定义 JSON、精确 POST 数和原始响应控制。
- 可切换到本机官方 Codex CLI，由 `codex exec` 为每个任务发起请求；不伪造 Codex 请求头或客户端元数据。
- 默认让每个任务使用独立的随机内容，也可关闭后改用固定提示词。
- 可编辑固定提示词、每批请求次数、额外批次重试次数、重试间隔、模型/地址覆盖、超时、Endpoint 模式和输出 token。
- 官方 CLI 模式展示任务摘要，实际请求头和请求体由 Codex CLI 生成；直接 API 模式展示实际 JSON，并可切换为自定义 JSON。
- 展示发送上限、批次进度、完成/失败数、首个成功响应、耗时和完整运行日志。
- 默认不生成 `config.json`、请求/响应日志、结果文件或锁文件。运行结果和完整请求日志只在应用内保留，需要时由用户显式导出。
- 默认在 `%LOCALAPPDATA%\CCSwitchBatchSender\logs\provider-diagnostics.jsonl` 保留容量受限的 provider 解析诊断，只记录指针、provider ID、快照状态、凭据来源类型和结果，不记录 API Key、URL、请求体或响应内容。
- 默认设置保存在当前用户注册表 `HKCU\Software\CCSwitchBatchSender`，不保存 API Key。
- 使用 Windows 命名互斥体防止重复启动，不产生 `run.lock`。

## 请求来源与重试语义

### 直接 API（默认）

“请求次数”是每批并发 POST 数；“重试次数”是首批全部失败后额外发送的批次数，不是对每个请求再次重试。

### 官方 Codex CLI

- “请求次数”是每批 Codex 任务数；每个任务由已安装的官方 `codex exec` 独立发起。
- “CLI 并发”限制同时运行的 Codex 进程数，默认 10。
- “重试次数”是整批没有成功结果后额外启动的任务批次数。
- 一次 Codex 任务内部实际产生多少 HTTP 请求、流式重连或官方重试由 Codex CLI 决定，因此界面不把任务数描述成精确 POST 数。
- 停止任务或关闭应用时，只终止本应用启动并登记的 Codex CLI 进程树，不影响 Codex Desktop 或其他 Codex 任务。
- 仅支持 Responses API provider。Chat Completions provider 需使用直接 API 模式。

应用通过单次子进程环境变量向 `codex exec` 提供所选 provider 的 API Key，并通过临时 `-c` 参数覆盖 model 和 base URL。Key 不进入命令行、日志、注册表或配置文件。

凭据读取与 CC Switch 3.18 保持一致：优先使用 `settings_config.auth.OPENAI_API_KEY`；没有时读取 active custom model provider 的 `experimental_bearer_token`，再回退顶层 token。活动 custom provider 的显式 Key 优先于 CC Switch 为兼容登录保留的 OAuth session；inactive provider section、官方 provider、`xai_oauth`、`PROXY_MANAGED` 和其他托管占位符仍不会被当作可直接发送的 API Key。

最大客户端 POST 数：

```text
请求次数 × (1 + 重试次数)
```

默认请求次数为 15，重试次数为 10，因此最多运行 11 批。直接 API 模式的默认上限是 165 个 POST；官方 CLI 模式的默认上限是 165 个逻辑任务。任意任务成功后不会再开启下一批；官方 CLI 模式会立即终止本应用登记启动的同批其他 Codex 进程树，不影响 Codex Desktop、手动启动的 CLI 或其他 Codex 会话。

终止本地 Codex CLI 任务会关闭它们到 provider/Sub2API 的连接，并可阻止支持请求取消传播的中继继续池重试或换号。关闭本地连接不等于撤销上游请求：已经被中继发送到上游的在途请求仍可能完成并计费，客户端无法撤销已经被上游接受的生成任务。

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
python -m pip install -r .\requirements.txt
python .\ccswitch_batch_sender.py --gui
```

只读检查当前 provider，不发送请求：

```powershell
python .\ccswitch_batch_sender.py --dry-run
```

临时关闭 provider 解析诊断日志：

```powershell
python .\ccswitch_batch_sender.py --gui --no-provider-diagnostics
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
python .\ccswitch_batch_sender.py --headless --transport direct
python .\ccswitch_batch_sender.py --headless --transport codex_cli --cli-concurrency 10
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
- 当前 provider 优先读取 `%USERPROFILE%\.cc-switch\settings.json` 的 `currentProviderCodex`，无有效指针时才回退到数据库唯一的 `is_current=1`；预览与运行会将该结果钉死为具体 provider ID。
- provider 凭据优先读取 `auth.OPENAI_API_KEY`，并兼容 CC Switch 3.18 的 active-provider/top-level `experimental_bearer_token`；活动 custom provider 的显式 Key 可覆盖保留的 OAuth session，结构化解析 TOML，绝不扫描 inactive section。
- 官方/OAuth/代理托管凭据和 `PROXY_MANAGED` 等占位符会 fail closed，不会进入 `CODEX_API_KEY`、`Authorization` 或日志。
- 不显示、不复制、不记录 API Key；注册表设置也不包含 Key。
- provider 诊断日志按 512 KiB 轮转并保留 2 份备份；字段名包含 Key、Token、Authorization、Password 或 Secret 的数据会被丢弃，写盘失败不影响请求流程。
- provider 或 CLI 返回内容进入日志和结果前，会再次按当前 API Key 做精确脱敏。
- provider 下拉只选择本工具的请求来源，不执行 CC Switch 的正式切换操作。
- 官方 CLI 模式由当前安装的 `codex exec` 发起请求并保留其自动元数据，不伪造任何官方字段。
- 直接 API 模式仅用于通用传输和自定义 JSON，始终明确标记为非 Codex 客户端。
- 自定义请求体和完整响应导出由用户显式启用；默认结果只包含脱敏 Endpoint、状态、耗时、文本和 usage。
