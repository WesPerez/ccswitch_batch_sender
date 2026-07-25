# CCSWITCH Batch Sender

这是一个独立于 Codex Desktop 会话的本地脚本：它只读本机
`%USERPROFILE%\.cc-switch\cc-switch.db`，读取当前 CCSWITCH Codex provider，
并发发送 20 个最小请求（默认消息为 `1`）。首个成功响应会立即写入
`latest-result.json` 并显示在界面中；其余 19 个连接继续等待，让 Sub2API 完成账号内重试、
池模式和换号。默认只发这一批 20 个，不增加客户端重试。

## 运行

双击 `run_ccswitch_batch_sender.bat` 即可。界面打开后会自动发送 20 个请求，
不需要修改配置，也不需要再点“开始”。界面会显示首个返回结果，并提供“打开结果”和“打开日志”按钮。

也可以在 PowerShell 中运行：

```powershell
python .\ccswitch_batch_sender.py --gui
```

无界面命令行模式：

```powershell
python .\ccswitch_batch_sender.py
```

只检查当前 provider、URL 和模型，不发送请求：

```powershell
python .\ccswitch_batch_sender.py --dry-run
```

需要一直等待到成功时：

```powershell
python .\ccswitch_batch_sender.py --until-success
```

## 配置

`config.json` 中的 `provider_id: "current"` 会在每一轮读取 CCSWITCH 当前 Codex provider，
因此切换 CCSWITCH 当前 provider 后，下一轮会自动使用新配置。脚本不保存、不复制 API key。

- `request_count`：每轮并发请求数，默认 20。
- `message`：请求内容，默认 `1`。
- `model` / `base_url`：留空表示读取当前 provider；填值时覆盖 provider 配置。
- `max_output_tokens`：最大输出 token，默认 1。
- `request_timeout_seconds`：单个 HTTP 请求默认等待 7200 秒（2 小时）。
- `max_wait_seconds`：整批默认最多等待 7200 秒（2 小时）。
- `retry_interval_seconds`：整轮无成功时，下一轮开始前的等待时间。
- `poll_interval_seconds`：provider 返回异步任务时的轮询间隔。
- `retry_batches`：默认 `false`，避免一次运行意外发送超过 20 个请求。
- `endpoint_style`：`auto` 会按 CCSWITCH 常见路径和 OpenAI `/v1` 路径依次尝试。
- `unique_prompt_cache_key`：为 20 个请求生成不同的缓存键；用户消息仍为 `1`。
- `db_path`：留空表示使用 `%USERPROFILE%\\.cc-switch\\cc-switch.db`。

## 输出与安全

- 日志：`logs\\run-YYYYMMDD.log`
- 首个成功结果：`latest-result.json`
- `run.lock` 防止同一时间启动第二批。
- 日志和结果不会写入 API key，也不会创建 Codex Desktop 会话。
- 请求使用真实客户端标识 `CCSWITCH Batch Sender/1.0`；没有伪装成 Codex Desktop。

如果 CCSWITCH 数据库不存在、provider 没有 API key/base URL/model，脚本会停止并在日志中给出原因。
