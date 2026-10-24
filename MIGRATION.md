# 余额阈值监控（云吉 Task 二开版）

用 **yunji-task**（`task.lijingang.ccwu.cc`）做调度和告警，每个 Metaso 账号一个任务，每 **20 分钟** 检查一次余额；低于阈值时告警，恢复时通知一次。全部跑在 Cloudflare Workers 免费额度内，**不产生费用**。

```
yunji-task（每 20 分钟） ──GET /check/账号A──> balance-check Worker ──> https://metaso.cn/api/v1/usage
        │                                            │
        │   200 = 余额正常（静默）                     └─ 取 JSON 里的余额数值，按阈值比较
        │   503 = 低于阈值 → 失败重试/告警  →  Webhook 邮件
        └─ 恢复（503→200）时再发一封“已恢复”
```

## 为什么需要二开

原版平台的限制（已核对源码）：执行器只有 HTTP/RPC，成功判定只能比对状态码、关键字或 JSON 等值/区间；告警模板拿不到响应体，也没有“恢复”语义。二开改动（`xiaoxiaoshuo/yunji-task-1`，`feat/check-executor` 已合入 `main`）：

| 文件 | 改动 |
|---|---|
| `src/executor/check.ts` | 新增「阈值检查」执行器：GET + JSON 路径取数 + 数值比较（lt/lte/gt/gte/eq/ne/between） |
| `src/executor/consumer.ts` | 检查类任务**只在进入失败时告警**；开启“恢复时也发通知”后，失败→成功再告警一次 |
| `src/executor/alert.ts` | 告警模板新增 `{{r.value}}`、`{{r.threshold}}`、`{{run.event}}`，新增 `task.recovered` 事件 |
| `migrations/0002_check_recovery.sql` | `tasks.notify_on_recover` 列（默认 0，老任务行为不变） |
| 后台表单/详情页 | 新增“阈值检查（余额等）”类型和对应的展示卡片 |
| `.github/workflows/verify.yml` | 每次推送跑 typecheck + 全部单测 + SPA 构建 |

## 部署 balance-check Worker

1. 本地先跑测试：`python3 -m unittest discover -s tests`（87 个用例，离线）。
2. 准备环境变量后执行 `python3 deploy_cf.py`：

| 变量 | 说明 |
|---|---|
| `CF_API_TOKEN` | Cloudflare API Token，权限 `Workers Scripts: Edit` |
| `CF_ACCOUNT_ID` | `302b72cf474e31fc9dcaf81abe023b1e` |
| `CHECK_TOKEN` | 自定的长随机串，yunji-task 调用时用 `Authorization: Bearer` 带上 |
| `ACCOUNTS_JSON` | 账号数组，见下 |

```json
[
  {"name": "账号A", "key": "mk-...", "threshold": 1500, "path": "data.balance"},
  {"name": "账号B", "key": "mk-...", "threshold": 1500}
]
```

可选字段：`operator`（默认 `gte`，语义为“≥ 阈值算正常”）、`thresholdTo`（`between` 用）、`path`（JSON 路径，如 `data.items[0].credits`）。

3. 自测：`curl -H "Authorization: Bearer $CHECK_TOKEN" https://<worker>/check/账号A`，正常 `200`，低于阈值 `503`，返回体里带 `value`、`threshold`，恢复的那一次带 `"recovered": true`。

> 本机 macOS 10.15 跑不了 workerd/esbuild，所以 `deploy_cf.py` 自己打包 Python 模块并用 Workers Scripts API 上传，不依赖 wrangler。

## 在云吉 Task 里配置（每个账号一个任务）

1. 「变量」页创建 `CHECK_TOKEN`（加密保存）。
2. 新建任务：名称 `余额-账号A`；调度「每隔 20 分钟」→ 生成 `0 */20 * * * *`；类型选**阈值检查（余额等）**；URL 填 `<worker>/check/账号A`；请求头 `Authorization: Bearer {{CHECK_TOKEN}}`；勾选“恢复时也发通知”。
3. 失败告警选「自定义 Webhook」，发到邮件中转（Bark/Resend 转邮件均可），正文模板示例：

```json
{
  "title": "[{{run.status}}] {{task.name}}",
  "body": "当前值 {{r.value}}，阈值 {{r.threshold}}；{{run.error}}；{{run.finished_at}}"
}
```

4. 多账号“汇总成一封邮件”：同样是一个账号一个任务（各自独立重试与状态），由中转脚本按事件收集后合并发送——平台侧不做跨任务汇总，避免给平台加脆弱的共享状态。

## 费用

| 项 | 用量（4 个账号 × 20 分钟） | 费用 |
|---|---|---|
| Worker 请求（yunji-task 异步执行 + balance-check） | ≈ 8.6k 次/月 | 免费额度 10 万/天，远未触顶 |
| Durable Objects（调度器 + 账号状态） | ≈ 1–2 万次请求/月 | Free 套餐含 DO 额度 |
| D1（日志 30 天、统计） | ≈ 数百 MB 内 | Free 5 GB/库 |

实际支出 **$0**；只要保持默认「Worker 异步」（≤25 秒）就避开直接执行的 DO 时长计费。
