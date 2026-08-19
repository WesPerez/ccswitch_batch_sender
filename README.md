# CC Switch Batch Sender

一个面向 Windows 的轻量批量请求工具。应用只读本机 CC Switch 的 Codex provider，默认跟随 CC Switch 当前上游，也可以在本次运行中选择其他 provider。

## 界面能力

- 打开和重置时优先选中名称包含 `any` 的第一个 Codex provider；没有匹配项时回退到 CC Switch 当前 provider。刷新和开始运行时重新读取当前指针和 provider 列表。
- 下拉标签、预览和启动校验使用同一 provider 快照；一次运行（包括额外重试批次）固定使用启动时选定的 provider，不会因 CC Switch 中途切换而漂移。
- 下拉切换只影响本工具本次发送，不写回 CC Switch，不修改 `cc-switch.db`。
- 默认使用“直接 API”模式，以保留完整自定义 JSON、精确 POST 数和原始响应控制。
- 可切换到本机官方 Codex CLI，由 `codex exec` 为每个任务发起请求；不伪造 Codex 请求头或客户端元数据。
- 直接 API 的客户端身份头按本机官方 `codex exec` 格式做兼容性模拟，不发送任何 `X-CCSwitch-*` 请求头。
- 默认让每个任务使用独立的随机内容，也可关闭后改用固定提示词。
- 可编辑固定提示词、每批请求次数、额外批次重试次数、重试间隔、模型/地址覆盖、超时、Endpoint 模式和输出 token；重试次数为 `0` 时持续重试。
- 官方 CLI 模式展示任务摘要，实际请求头和请求体由 Codex CLI 生成；直接 API 模式展示实际 JSON，并可切换为自定义 JSON。
- 展示有限发送上限或无限重试状态、批次进度、完成/失败数、首个成功响应、耗时和完整运行日志。
- 首次成功后发送 Windows 通知；默认进入定时保持状态，每 3 分钟只发送 1 个请求。界面显示下次请求倒计时、定时成功/失败计数，日志按每次定时请求记录结果，点击“停止”可立即结束。
- “成功后定时保持”和保持间隔可在主界面直接调整并随“保存默认”持久化；间隔按分钟显示，默认 3 分钟。
- 运行日志会对失败、取消和首个成功请求记录限长且脱敏的响应摘要，便于判断上游实际返回格式。
- 自动 Endpoint 模式优先标准 `/v1/responses`；若候选地址返回 HTML 页面会继续尝试备用地址，全部返回 HTML 时停止无意义重试并明确报错。
- 默认在 `%LOCALAPPDATA%\CCSwitchBatchSender\settings.json` 生成可读 JSON 配置；运行结果和完整请求日志只在应用内保留，需要时由用户显式导出。
- 默认在 `%LOCALAPPDATA%\CCSwitchBatchSender\logs\provider-diagnostics.jsonl` 保留容量受限的 provider 解析诊断，只记录指针、provider ID、快照状态、凭据来源类型和结果，不记录 API Key、URL、请求体或响应内容。
- 默认设置保存在当前用户文件 `%LOCALAPPDATA%\CCSwitchBatchSender\settings.json`，不保存 API Key。升级时会先写入该文件，再删除旧版 `HKCU\Software\CCSwitchBatchSender` 注册表键。
- “应用设置”窗口会直接显示本地保存的重试、重试间隔、单请求超时和总等待；修改后保存会写回配置文件。
- 升级迁移会把旧版默认的“重试 10 次 / 单次超时 7200 秒 / 总等待 7200 秒”更新为新版默认 `0 / 10 / 0`；用户明确保存的其他有限值保持不变。
- 使用 Windows 命名互斥体防止重复启动，不产生 `run.lock`。

## 请求来源与重试语义

### 直接 API（默认）

“请求次数”是每批并发 POST 数；“重试次数”是首批全部失败后额外发送的批次数，不是对每个请求再次重试。`0` 表示无限批次，直到成功、手动停止、达到可选总等待时间或遇到不可重试错误。

单个直接 API 请求默认 10 秒无响应即超时，避免少量失联连接长期阻塞整批；超时只记该请求失败并按重试规则继续，不会作为当前批次或整次运行的截止时间。总等待时间仍独立限制整次运行；若底层请求线程自身未按 deadline 返回，应用会将该请求记为未完成，全部发送槽位均被占用时等待槽位释放或手动停止。

### 官方 Codex CLI

- “请求次数”是每批 Codex 任务数；每个任务由已安装的官方 `codex exec` 独立发起。
- “CLI 并发”限制同时运行的 Codex 进程数，默认 10。
- “重试次数”是整批没有成功结果后额外启动的任务批次数；`0` 表示无限批次。
- 一次 Codex 任务内部实际产生多少 HTTP 请求、流式重连或官方重试由 Codex CLI 决定，因此界面不把任务数描述成精确 POST 数。
- 停止任务或关闭应用时，只终止本应用启动并登记的 Codex CLI 进程树，不影响 Codex Desktop 或其他 Codex 任务。
- 仅支持 Responses API provider。Chat Completions provider 需使用直接 API 模式。

应用通过单次子进程环境变量向 `codex exec` 提供所选 provider 的 API Key，并通过临时 `-c` 参数覆盖 model 和 base URL。Key 不进入命令行、日志、旧注册表或配置文件。

凭据读取与 CC Switch 3.18 保持一致：优先使用 `settings_config.auth.OPENAI_API_KEY`；没有时读取 active custom model provider 的 `experimental_bearer_token`，再回退顶层 token。活动 custom provider 的显式 Key 优先于 CC Switch 为兼容登录保留的 OAuth session；inactive provider section、官方 provider、`xai_oauth`、`PROXY_MANAGED` 和其他托管占位符仍不会被当作可直接发送的 API Key。

重试次数大于 `0` 时，最大客户端 POST/任务数为：

```text
请求次数 × (1 + 重试次数)
```

默认请求次数为 15，重试次数为 `0`，总等待时间也为 `0`，因此默认会按批持续重试，直到成功或用户手动停止。鉴权、参数和 Endpoint 等明确不可重试错误仍会直接终止。任意任务成功后不会再开启下一批；官方 CLI 模式会立即终止本应用登记启动的同批其他 Codex 进程树，不影响 Codex Desktop、手动启动的 CLI 或其他 Codex 会话。

GUI 默认启用“成功后定时保持”：首次成功并结束当前批次后，每隔 3 分钟复用本次固定的 provider 和请求配置发送 1 个任务。保持请求失败只记录日志并等待下一轮，不会重新启动整批；停止或关闭应用会取消后续定时请求。无界面模式仍在首次成功后退出，不会隐式驻留。

首次成功时，直接 API 模式会关闭同批其他请求的本地连接并短暂等待线程回收，官方 CLI 模式会终止本应用启动的同批进程。只有原批次任务都已回收后才进入定时保持，避免旧请求与 3 分钟请求重叠；若底层请求仍无法结束，界面会明确提示保持暂未启动。成功日志包含 `success_at`，右侧成功结果也显示成功时间。

终止本地 Codex CLI 任务会关闭它们到 provider/Sub2API 的连接，并可阻止支持请求取消传播的中继继续池重试或换号。关闭本地连接不等于撤销上游请求：已经被中继发送到上游的在途请求仍可能完成并计费，客户端无法撤销已经被上游接受的生成任务。

## 请求体与随机任务

- 官方 CLI 模式只把用户任务交给 `codex exec`，完整 Responses 请求由官方客户端构造。
- 直接 API 的自动 Responses 请求只把任务放入 `input`，不再额外生成 `instructions: "Return ..."`。
- 随机任务来自小型自然任务池，覆盖计算、排序、单位换算、文本转换和结构化输出；只有 HTTP 成功且返回 JSON 通过语义校验时才计为成功。
- 默认输出上限为 64 token。关闭“每次随机任务”后，应用使用左侧编辑的固定提示词。
- 直接 API 自动请求中的 `<每个请求唯一>` 和 `<每个请求随机任务>` 是预览占位符，每个 POST 发送前分别替换为 UUID 和本次真实任务；旧的 `<每个请求随机探针>` 仍兼容。
- 自定义 JSON 也识别这两个精确占位符；固定的 `prompt_cache_key` 会原样保留，模板内容不会被运行时替换污染。
- 自动生成模式下请求体整体只读；自定义模式下，包含自动替换占位符的 JSON 行会高亮，并在工具栏标明发送时替换。

直接 API 模式读取本机 `codex --version`，并按本机官方 `codex exec 0.146.0` 的实际 POST 请求格式生成两个兼容身份头：

```text
User-Agent: codex_exec/<本机版本> (Windows <系统版本>; <架构>) unknown (codex_exec; <本机版本>)
Originator: codex_exec
```

直接 API 不再发送 `X-CCSwitch-Local-Codex-CLI-Version`，界面和配置中也没有对应开关。这里仅复刻 `User-Agent` 与 `Originator` 的兼容格式，不生成 `session-id`、`thread-id`、`x-codex-turn-metadata` 等真实 Codex 会话元数据，因此仍属于兼容性模拟，不等同于官方 Codex CLI 请求。官方 CLI 模式的全部身份和会话元数据仍由本机 `codex exec` 自行生成。

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

`--config path.json` 仅作为临时高级入口保留；默认设置文件固定为 `%LOCALAPPDATA%\CCSwitchBatchSender\settings.json`。

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
- 不显示、不复制、不记录 API Key；JSON 设置文件与被迁移删除的旧注册表设置都不包含 Key。
- provider 诊断日志按 512 KiB 轮转并保留 2 份备份；字段名包含 Key、Token、Authorization、Password 或 Secret 的数据会被丢弃，写盘失败不影响请求流程。
- provider 或 CLI 返回内容进入日志和结果前，会再次按当前 API Key 做精确脱敏。
- provider 下拉只选择本工具的请求来源，不执行 CC Switch 的正式切换操作。
- 官方 CLI 模式由当前安装的 `codex exec` 发起请求并保留其自动元数据，不伪造任何官方字段。
- 直接 API 模式仅模拟本机 `codex exec` 的 `User-Agent` 与 `Originator` 兼容格式；请求仍由本应用发送，不作为官方 CLI 烟测结果。
- 自定义请求体和完整响应导出由用户显式启用；默认结果只包含脱敏 Endpoint、状态、耗时、文本和 usage。
