# API balance email monitor

使用 GitHub Actions 定时查询一个 HTTPS JSON 余额接口，通过 [Resend](https://resend.com/docs/api-reference/emails/send-email) 发送邮件报警。Python 3.12 标准库实现，无第三方 Python 依赖。

## 行为

- 每 5 分钟计划检查一次，cron 为 `*/5 * * * *`（每小时第 0、5、10……55 分钟）；实际触发可能延迟，不能将此配置视为执行频率保证。
- 接口应返回 JSON 对象，例如 `{"balance": 1234.56}`。`balance` 必须是有限数值，不能是字符串或布尔值。
- 默认余额 **严格低于 1500** 时报警；等于 1500 不报警。阈值可通过仓库变量配置。
- 接口超时、网络故障、非成功 HTTP 状态、无效 JSON、缺失或无效余额字段均触发邮件报警。
- 超时、网络故障、HTTP 408/425/429/5xx 最多尝试 3 次，每次 socket 操作超时 15 秒，重试间隔为 1、2 秒。整个余额查询另有 60 秒总时限，防止缓慢滴流响应耗尽作业时间；发信阶段另有 60 秒总时限。鉴权失败等不可重试错误立即报警。
- **每次发现异常都发邮件，不做跨次去重或冷却。** 正常时不发邮件，也不额外发送恢复通知。
- 同一封邮件的网络重试使用相同 Resend 幂等键，降低重复投递风险。
- 极端大小或精度的数值使用有界科学计数法展示，避免异常数值把邮件正文放大。
- 手动 `test-email` 模式始终发送一封测试邮件，并在邮件中展示本次接口结果。

## 配置

在仓库 Settings → Secrets and variables → Actions 中设置以下 **Secrets**。不要将真实值提交到代码、Issue、日志或 Artifact。

| Secret | 用途 |
| --- | --- |
| `BALANCE_API_URL` | HTTPS 余额接口完整 URL |
| `BALANCE_API_TOKEN` | 接口 Bearer Token，值中不要加 `Bearer ` 前缀 |
| `RESEND_API_KEY` | Resend 发信 API Key，生产环境建议最小权限 |
| `ALERT_FROM` | 已在 Resend 验证的发件地址，可包含显示名称 |
| `ALERT_TO` | 一个收件邮箱 |

设置以下仓库 **Variables**：

| Variable | 值 |
| --- | --- |
| `BALANCE_THRESHOLD` | `1500`，有限且非负的数值 |
| `MONITOR_ENABLED` | 初始为 `false`；设为 `true` 才允许定时执行 |

`MONITOR_ENABLED` 未设置、为空或不等于 `true` 时，所有计划任务跳过（GitHub 表达式比较不区分大小写）。手动运行仍可用于验证。

## 手动验证与启停

1. 配置 Secrets 和 Variables，暂时保持 `MONITOR_ENABLED=false`。
2. 在 Actions → Balance monitor → Run workflow 中选择：
   - `check`：真实查询一次，只在异常时发邮件。
   - `test-email`：真实查询并发送一封测试邮件，不自动启用定时任务。
3. 检查收件箱及 Resend 投递状态。API 接受邮件不等于最终进入收件箱。
4. 确认无误后，将 `MONITOR_ENABLED` 设为 `true`。
5. 暂停时，将其设为 `false`。已经开始的运行不会因此自动取消；必要时在 Actions 页面另行取消。

也可通过 GitHub CLI 操作（在仓库目录中）：

```sh
gh variable set MONITOR_ENABLED --body false
gh workflow run monitor.yml -f mode=check
gh workflow run monitor.yml -f mode=test-email
# 仅在确认后开启：
gh variable set MONITOR_ENABLED --body true
```

## 安全与可靠性边界

- 不在公开代码或运行日志中输出真实 URL、Token、邮箱、余额、响应正文或异常堆栈；余额和安全的错误概述只通过邮件发送。
- 两个 API 的请求均禁用重定向，使用默认 HTTPS 证书验证，响应读取限制为 64 KiB。
- Actions 仅有 `contents: read` 权限；依赖 Action 固定到提交 SHA；测试工作流不获取 Secrets。
- 同一时刻只运行一个监控作业。作业最多运行 3 分钟。
- **GitHub Actions 是尽力调度，不保证每 5 分钟准点执行**；高负载时可能延迟或丢弃计划任务。参见 [GitHub schedule 说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
- 公开仓库长时间无仓库活动时，GitHub 可能自动停用计划工作流（官方规则为 60 天）。请定期检查并在需要时重新启用。
- 公开仓库的标准 GitHub 托管 runner 免费，但 Resend 有独立套餐、速率和发送额度限制。持续异常最多约 **288 封/天**，请确保邮件额度足够。
- 如果 Resend 故障、邮件额度耗尽或整个 Actions 未运行，就无法依靠同一通道发送报警。邮件提交失败时作业失败，需另行关注 Actions 和 Resend 状态；此项目不是外部心跳监控。
- 密钥曾在聊天或其他位置公开时，建议轮换并更新对应 Secrets。不要把真实凭证放入本地配置文件后提交。

## 本地测试

以下测试完全离线，不调用真实接口，不发送邮件，也不需要任何 Secrets：

```sh
python -m unittest discover -s tests -v
```
