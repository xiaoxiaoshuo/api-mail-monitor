# API balance email monitor

定时查询一个 HTTPS JSON 余额接口，通过 [Resend](https://resend.com/docs/api-reference/emails/send-email) 发送邮件报警。Python 3.12 标准库实现，无第三方 Python 依赖。

两种调度方式共用同一份 `monitor.py`：

- **本机 Windows 任务计划（推荐，每 5 分钟真实触发）**
- **GitHub Actions（低频备份，实测平均约 3 小时一次）**

## 行为

- 本机任务每 5 分钟检查一次；GitHub 的 cron 虽写成 `*/5 * * * *`，但公开仓库的 `schedule` 会被 GitHub 限流，实测间隔 1.75～5 小时，**不能当作 5 分钟监控**。
- 接口应返回 JSON 对象，例如 `{"balance": 1234.56}`。`balance` 必须是有限数值，不能是字符串或布尔值。
- 默认余额 **严格低于 1500** 时报警；等于 1500 不报警。阈值可通过配置调整。
- 接口超时、网络故障、非成功 HTTP 状态、无效 JSON、缺失或无效余额字段均触发邮件报警。
- 超时、网络故障、HTTP 408/425/429/5xx 最多尝试 3 次，每次 socket 操作超时 15 秒，重试间隔为 1、2 秒。整个余额查询另有 60 秒总时限，防止缓慢滴流响应耗尽作业时间；发信阶段另有 60 秒总时限。鉴权失败等不可重试错误立即报警。
- **可选的提醒冷却**：设置 `ALERT_COOLDOWN_MINUTES=60` 后，同一类异常每小时最多提醒一次；异常类型改变（如接口故障转余额不足）会立即提醒；恢复正常时再发一封恢复通知。未配置冷却时保持“每次运行都发”。
- 同一封邮件的网络重试使用相同 Resend 幂等键，降低重复投递风险。
- 极端大小或精度的数值使用有界科学计数法展示，避免异常数值把邮件正文放大。
- 手动 `test-email` 模式始终发送一封测试邮件，并在邮件中展示本次接口结果。

## 配置

### 环境变量

| 变量 | 必填 | 用途 |
| --- | --- | --- |
| `BALANCE_API_URL` | 是 | HTTPS 余额接口完整 URL |
| `BALANCE_API_TOKEN` | 是 | 接口 Bearer Token，值中不要加 `Bearer ` 前缀 |
| `RESEND_API_KEY` | 是 | Resend 发信 API Key |
| `ALERT_FROM` | 是 | 已在 Resend 验证的发件地址，可包含显示名称 |
| `ALERT_TO` | 是 | 一个收件邮箱 |
| `BALANCE_THRESHOLD` | 否 | 报警阈值，默认 `1500` |
| `ALERT_COOLDOWN_MINUTES` | 否 | 同类异常重复提醒的最小间隔分钟数，默认 `0`（不去重） |
| `ALERT_STATE_FILE` | 否 | 冷却状态文件路径；设置后才启用去重和恢复通知 |

`ALERT_STATE_FILE` 不存在或损坏时会照常发信，只会多提醒、不会漏提醒。

### 本机 Windows 部署

1. 把 5 个必填变量写入**当前用户环境变量**（不要写进仓库或明文脚本）。
2. 用任务计划每 5 分钟调用一个包装脚本，脚本从 `HKCU:\Environment` 读取变量后执行 `monitor.py`。
3. 任务设置“忽略重叠实例”，避免上一次未结束时叠加。

参考命令：

```powershell
# 设为当前用户变量（不要放进公开仓库）
[Environment]::SetEnvironmentVariable('BALANCE_API_TOKEN', '<token>', 'User')

# 每 5 分钟触发一次
schtasks /create /tn MetasoBalanceMonitor /sc minute /mo 5 /tr "powershell -NoProfile -WindowStyle Hidden -File <runner.ps1>"
```

本机模式的限制：任务以当前用户交互身份运行，**必须处于登录状态**；电脑关机或休眠期间不会检查。若需要关机期间的兜底，可保留 GitHub 备份。

### GitHub Actions 备份配置

在仓库 Settings → Secrets and variables → Actions 中设置同名 **Secrets**，并设置仓库 **Variables**：

| Variable | 值 |
| --- | --- |
| `BALANCE_THRESHOLD` | `1500` |
| `MONITOR_ENABLED` | 设为 `true` 才允许定时执行 |

GitHub 端不设置 `ALERT_COOLDOWN_MINUTES` / `ALERT_STATE_FILE`（无法跨运行保存状态），因此每次运行都会发信；因为频率只有约 3 小时一次，总量仍然很低。

## 手动验证与启停

GitHub 手动运行：在 Actions → Balance monitor → Run workflow 中选择 `check`（只在异常时发信）或 `test-email`（始终发一封测试邮件）。

```sh
gh workflow run monitor.yml -f mode=check
gh workflow run monitor.yml -f mode=test-email
gh variable set MONITOR_ENABLED --body false   # 暂停 GitHub 定时
```

本机暂停/恢复：

```powershell
Disable-ScheduledTask -TaskName MetasoBalanceMonitor
Enable-ScheduledTask  -TaskName MetasoBalanceMonitor
```

## 安全与可靠性边界

- 不在公开代码或运行日志中输出真实 URL、Token、邮箱、余额、响应正文或异常堆栈；余额和安全的错误概述只通过邮件发送。
- 两个 API 的请求均禁用重定向，使用默认 HTTPS 证书验证，响应读取限制为 64 KiB。
- Actions 仅有 `contents: read` 权限；依赖 Action 固定到提交 SHA；测试工作流不获取 Secrets。
- 同一时刻只运行一个监控作业。作业最多运行 3 分钟。
- **GitHub Actions 是尽力调度，不保证每 5 分钟准点执行**；高负载时可能延迟或丢弃计划任务。参见 [GitHub schedule 说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
- 公开仓库长时间无仓库活动时，GitHub 可能自动停用计划工作流（官方规则为 60 天）。请定期检查并在需要时重新启用。
- 公开仓库的标准 GitHub 托管 runner 免费，但 Resend 有独立套餐、速率和发送额度限制：**免费账户为 100 封/天、3000 封/月**，含收件。因此每 5 分钟一次且不做去重会在约 8 小时后耗尽当日额度并导致发信失败。本机部署建议使用 `ALERT_COOLDOWN_MINUTES=60`（异常时最多 24 封/天）。
- 如果 Resend 故障、邮件额度耗尽或本机/定时任务未运行，就无法依靠同一通道发送报警；此项目不是外部心跳监控。
- 密钥曾在聊天或其他位置公开时，建议轮换并更新对应配置。不要把真实凭证放入本地配置文件后提交。

## 本地测试

以下测试完全离线，不调用真实接口，不发送邮件，也不需要任何 Secrets：

```sh
python -m unittest discover -s tests -v
```
