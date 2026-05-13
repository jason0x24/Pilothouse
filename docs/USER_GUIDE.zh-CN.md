# Pilothouse 使用指南

[English](./USER_GUIDE.md) · **简体中文**

写给**运维方**的端到端指南 —— 怎么把 Pilothouse 跑起来、怎么配
Agent、怎么触发它们、怎么审批破坏性操作、怎么管租户。

如果你想**写插件**,看 [PLUGIN_AUTHORING.zh-CN.md](./PLUGIN_AUTHORING.zh-CN.md)。

## 1. Pilothouse 是什么(一段话)

Pilothouse 是一个**按触发器跑 LLM Agent**的服务器。你注册一个
**Agent**(name + 一个 *template*(用哪个剧本)+ 参数 + 可选的 cron
schedule)。从此当某件事情发生时(cron 到点 / webhook 进来 / 你手动
触发),agent 的 template 告诉 LLM 怎么做,LLM 调用注册过的
**connector** 里的**工具**(Datadog、GitHub 等),Pilothouse 把每一
步都记进 append-only 审计日志。任何破坏性操作(发评论、删东西)要么
走 **dry-run**(仅预览),要么走 **审批**(人决定)。从 day one 就
是多租户的。

## 2. 安装

### 本地 Python:`uv`(推荐)

[`uv`](https://docs.astral.sh/uv/) 是最快的路径:自动准备合适的
Python 解释器、管理 venv、还会生成可复现的 `uv.lock`。仓库里
`.python-version` 已经钉到 `3.12`,所以第一次 sync 会自动下载对应的
CPython。

```bash
# 1. 安装 uv(每台机器装一次)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. clone + 同步所有 extras(dev + temporal)
git clone <repo>
cd Pilothouse
uv sync --all-extras
```

所有命令通过 `uv run` 跑,不需要手动 `activate`:

```bash
uv run pilothouse db init        # 初始化 SQLite + default 租户
uv run pilothouse demo           # mock 模式端到端 demo(不需要 API key)
uv run pytest -q                 # 跑完整测试集(150+ 用例)
```

常用后续操作:

```bash
uv add httpx                     # 加运行时依赖(会更新 uv.lock)
uv add --group dev pytest-xdist  # 加只在 dev 用的依赖
uv sync                          # 严格按 lock 文件复现环境
uv lock --upgrade                # 在 pyproject 区间内升级所有依赖
```

### 本地 Python:`pip`(传统方式)

不想用 uv,继续用 pip / venv 也行:

```bash
git clone <repo>
cd Pilothouse
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .             # 只用 in-process executor
# 或
pip install -e '.[temporal]' # 同时启用 Temporal 模式(durable workflow)

pilothouse db init
pilothouse demo
```

### 日常用法对照:`uv` vs `pip`

| 任务 | uv | pip / venv |
|---|---|---|
| 激活环境 | (不需要,用 `uv run …`) | `source .venv/bin/activate` |
| 跑命令 | `uv run pilothouse …` | `pilothouse …`(venv 里) |
| 跑测试 | `uv run pytest -q` | `pytest -q` |
| 加运行时依赖 | `uv add foo` | 改 `pyproject.toml` + `pip install -e .` |
| 加 dev 依赖 | `uv add --group dev foo` | 改 `pyproject.toml` + 重装 |
| 升级所有依赖 | `uv lock --upgrade && uv sync` | `pip install -U -e '.[dev,temporal]'` |
| 切换 Python 版本 | 改 `.python-version`,`uv sync` | 用新解释器重建 venv |
| 可复现安装 | `uv sync`(读 `uv.lock`) | `pip install -e .`(无 lock) |

`uv.lock` 是提交到 git 的 —— 每个贡献者、每次 CI 看到的依赖树都
**完全一致**。

### Docker(api + console + Postgres)

```bash
docker compose up
# api      http://localhost:8088
# console  http://localhost:3000
```

compose 栈把 Postgres 接成数据库,同时暴露 API 和控制台。在
`up` 之前用你的 shell 环境覆盖 `PILOTHOUSE_ANTHROPIC_API_KEY` 和
connector 凭证即可。

## 3. 心智模型

三个名词:

| | 含义 | 例子 |
|---|---|---|
| **Template(模板)** | Agent 跑的剧本。定义 system prompt、允许的工具、还有一个确定性的 `mock_plan` 让你不花 LLM credit 就能跑。代码交付 | `datadog_alert_triage`、`bug_auto_fixer` |
| **Connector(连接器)** | LLM 能调用的**工具**的命名包。工具分只读和破坏性两种。代码或 MCP server 交付 | `github`、`datadog`、`linear` |
| **Agent** | 一个 template 的实例,带你的参数 + 触发方式。**运维只需要建这个** | "调查 `checkout` 告警,把结果发到 `#sre`" |

每个 **Run** 是一个 Agent 的一次执行。Run 有 status
(`running`、`awaiting_approval`、`succeeded`、`failed`、`cancelled`)、
事件日志、还有(可能的)关联审批。

## 4. 你的第一个 Agent

三种方式建,挑顺手的。

### 4.1 CLI

```bash
pilothouse agents create checkout-triage datadog_alert_triage \
    --param service=checkout \
    --param 'slack_channel="#sre-checkout"' \
    --no-dry-run                               # 破坏性工具走审批门
```

查看:

```bash
pilothouse agents show checkout-triage
pilothouse agents list
```

手动触发:

```bash
echo '{"alert_id":"abc-123","service":"checkout"}' \
    | pilothouse agents trigger checkout-triage
```

### 4.2 HTTP API

```bash
curl -X POST http://127.0.0.1:8088/agents \
    -H 'content-type: application/json' \
    -d '{"name":"checkout-triage",
         "template":"datadog_alert_triage",
         "params":{"service":"checkout","slack_channel":"#sre-checkout"},
         "dry_run":false}'
```

触发:

```bash
curl -X POST http://127.0.0.1:8088/agents/<id>/trigger \
    -d '{"payload":{"alert_id":"abc-123"}}'
```

### 4.3 GitOps —— `agents.yaml`

**生产环境推荐这个**。把 `agents.yaml` 提交 git,让 CI apply。
plan 输出是 tf/kubectl 风格的 `+ add`、`~ change`、`- delete`。

```yaml
# agents.yaml
version: 1
defaults:
  dry_run: true
prune: false                              # true = 删掉文件里没的 Agent
agents:
  - name: checkout-triage
    template: datadog_alert_triage
    description: 调查 checkout 延迟告警
    params:
      service: checkout
      slack_channel: "#sre-checkout"
      notify_slack_channel: "#sre-approvals"     # 审批通知去哪
  - name: nightly-flaky-scan
    template: flaky_test_hunter
    params:
      repo: acme/api
      tracking_issue: 42
    schedule_cron: "0 5 * * *"
```

```bash
pilothouse plan  -f agents.yaml                      # 预览
pilothouse apply -f agents.yaml --auto-approve       # CI 友好
pilothouse export -o agents.yaml                     # 反向导出当前状态
```

HTTP 上同样的接口:`/manifest/{plan,apply,export}`。

## 5. 触发 Agent

| 方式 | 适合 |
|---|---|
| **手动(CLI)** | `pilothouse agents trigger <id> --file event.json` —— 测试、一次性 run |
| **手动(HTTP)** | `POST /agents/{id}/trigger` —— 脚本 / 内部 dashboard |
| **Cron** | 给 Agent 设 `schedule_cron`;APScheduler 触发。`/schedule` 端点看下次触发时间 |
| **Webhook(Datadog)** | `POST /webhooks/datadog/{agent_id}` —— `DD-Signature` 签名校验 |
| **Webhook(GitHub)** | `POST /webhooks/github/{agent_id}` —— `X-Hub-Signature-256` 签名校验 |
| **Webhook(PagerDuty)** | `POST /webhooks/pagerduty/{agent_id}` —— `X-PagerDuty-Signature` 多 key(支持轮换) |
| **Webhook(Slack)** | `POST /webhooks/slack/{agent_id}` —— Slack v0 + 5 分钟时间窗 |
| **Webhook(Alertmanager / generic)** | `POST /webhooks/{alertmanager,generic}/{agent_id}` —— 通用 HMAC |
| **自定义(plugin)** | 自己写一个 `TriggerPlugin` —— Kafka 消费、文件监听,什么都行 |

每个 webhook source 有独立的密钥 env var(`PILOTHOUSE_<SOURCE>_WEBHOOK_SECRET`),
方便单独轮换。空 secret = 跳过校验(对开发友好的默认)。

### 触发去重

webhook 重试(Datadog/GitHub 在 5xx 时会重试)在
`PILOTHOUSE_DEDUP_WINDOW_SECONDS`(默认 60s)窗口内合并。同 Agent +
同 payload 摘要返回已有的 run_id,不重新启动。

### 速率限制

每租户滑动 60 秒窗口,`PILOTHOUSE_RATE_LIMIT_PER_MINUTE`(默认 60)。
超过返回 HTTP 429。

## 6. 审批流程

默认每个破坏性工具都走审批门。两层:

```
触发 → agent → 工具调用 → 是不是破坏性?
                            │
               ┌────────────┴────────────┐
               │                         │
         dry_run=true               dry_run=false
               │                         │
        "would_have_called   require_approval_for_writes?
         with: …" 预览                  │
               │                ┌───────┴───────┐
               │                │               │
               │            true(默认)        false
               │                │               │
               │           暂停 + 建 Approval  直接执行
               │                │
               │           等人决定
               │                │
               └────────────────┴── run 完成
```

### 看待审批

```bash
pilothouse approvals list                       # 默认只显示 pending
pilothouse approvals show <approval-id>         # 完整 payload + 模型的理由
```

或者控制台:`/approvals` 路由,支持复选批量操作。

### 审批

```bash
pilothouse approvals approve <approval-id> --by alice
pilothouse approvals reject <approval-id> --by alice --reason "先轮换 key"

# 批量:
pilothouse approvals approve-all --tool github_post_pr_comment --by alice
pilothouse approvals reject-all --agent scanner-foo --reason "PR 作者自己修"
```

最后一个待审批解决后,run 自动 resume。

### Slack 原生审批

在 Agent 上设 `notify_slack_channel: "#sre"`。Agent 请求审批时,
Slack 频道里出现一条带 Approve / Reject 按钮的消息。点击 POST 回签名
webhook,解析审批,**原地**更新消息。**不用离开 Slack** 就能批准
PR 评论或 runbook 步骤。

配置:

1. 在 workspace 装上 Slack bot,给 `chat.write` + `commands` scope
2. `export PILOTHOUSE_SLACK_BOT_TOKEN=xoxb-…`
3. 在 Slack app 的 **Interactivity** 里订阅
   `https://<pilothouse-host>/webhooks/slack/interactivity`
4. `export PILOTHOUSE_SLACK_SIGNING_SECRET=…` 让 Pilothouse 校验 v0 签名

### 审批 TTL

待审批超过 `PILOTHOUSE_APPROVAL_TTL_MINUTES`(默认 60)被后台
sweeper 自动 reject。run 用结构化的"过期"拒绝结果给 LLM,模型可以
自己决定怎么办(通常:记日志后退出)。**永远不会有挂死的审批**。

## 7. 监控 Run

### Run 搜索

```bash
# CLI
pilothouse runs show <run-id>
pilothouse runs logs <run-id>            # 着色时间线
pilothouse runs logs <run-id> --kind tool_call --tail 20

# HTTP —— 租户级搜索
curl 'http://127.0.0.1:8088/runs?status=succeeded&agent=checkout-triage&limit=20'
```

### 实时事件流(SSE)

```
GET /runs/{id}/events/stream
```

先回放整个 run 的历史事件,然后挂到内部 bus 接实时事件。terminal 后
发 `event: end` 关闭。控制台 run 详情页用这个滚动时间线。

### 审计导出

```
GET /runs/{id}/export.json       # 完整审计包(run + agent + events + approvals)
GET /runs/{id}/export.csv        # 事件时间线 CSV
```

直接贴到 SOC2 工单 —— "AI 做了什么,什么时候,谁批的"全在里面。

### 生命周期控制

```bash
pilothouse runs cancel <run-id> --by alice          # 中途取消
pilothouse runs retry <run-id>                      # 用同 payload 重跑
```

取消是协作式的 —— runtime 在下次循环边界退出。run 在审批门暂停时,
所有 pending 审批以"Run cancelled"原因被 reject。

### 仪表盘 + metrics

- 控制台 **`/dashboard`** —— 每日花费、按 Agent 排序的成本、按 status
  分布,可配置窗口(1d / 7d / 30d)
- **`GET /stats?days=N`** —— 同样的数据 JSON 格式给你自己的 dashboard
- **`GET /metrics`** —— Prometheus 文本格式。事件/工具/审批/run terminal
  的 counter,agents + 待审批 + 暂停 run 的 gauge

## 8. 多租户

单租户安装零变化 —— 启动自动建 `default` 租户,旧的
`PILOTHOUSE_API_KEYS` 环境变量灌入它的 keys。

要切出多个租户:

```bash
pilothouse tenants create acme --display-name "Acme Corp"
pilothouse tenants add-key acme                    # 自动生成 `phk_…` key(只打印一次)
pilothouse tenants add-key acme --key existing-key
pilothouse tenants set-quota acme \
    --max-agents 10 \
    --max-runs-per-day 500
pilothouse tenants show-keys acme                  # mask 显示
```

控制台 / 外部客户端用以下任一方式认证:

```
Authorization: Bearer <key>
X-API-Key: <key>
```

跨租户查询返回 **404**(不是 403)—— Pilothouse 不会泄露某 id 在另
一租户里是否存在。

### 租户管理模型

所有租户 CRUD 都走 `pilothouse tenants` —— **绝不通过 HTTP**。
这样泄露的租户 key 永远无法升权到其它租户或新建租户;只有拥有
Pilothouse 主机 shell 权限的运维能动。

## 9. 配置参考

所有设置都是 `PILOTHOUSE_` 前缀的环境变量。自动加载 `.env`。

### 核心

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PILOTHOUSE_ANTHROPIC_API_KEY` | `""` | 设了用真实 Claude,空用 mock 模式 |
| `PILOTHOUSE_MODEL_PLANNER` | `claude-opus-4-5` | planner 模型 |
| `PILOTHOUSE_MODEL_WORKER` | `claude-haiku-4-5` | 高频小任务模型 |
| `PILOTHOUSE_DATABASE_URL` | `./var` 下 sqlite | SQLAlchemy URL |
| `PILOTHOUSE_HOST` / `PILOTHOUSE_PORT` | `127.0.0.1` / `8088` | HTTP 监听 |

### 鉴权 + 安全

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PILOTHOUSE_REQUIRE_APPROVAL_FOR_WRITES` | `true` | 破坏性工具走审批门 |
| `PILOTHOUSE_API_KEYS` | `""` | 旧版:逗号分隔,bootstrap 灌入 default 租户 |
| `PILOTHOUSE_APPROVAL_TTL_MINUTES` | `60` | 待审批多久后自动 reject |
| `PILOTHOUSE_DEDUP_WINDOW_SECONDS` | `60` | 触发去重窗口 |
| `PILOTHOUSE_RATE_LIMIT_PER_MINUTE` | `60` | 每租户触发上限 |

### Executor

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PILOTHOUSE_TEMPORAL_ADDRESS` | `""` | `""` = in-process;`dev` = 进程内 Temporal server;`host:7233` = 集群 |
| `PILOTHOUSE_TEMPORAL_NAMESPACE` | `default` | Temporal namespace |
| `PILOTHOUSE_TEMPORAL_TASK_QUEUE` | `pilothouse` | worker task queue |

### Connector 凭证

| 变量 | 启用 |
|---|---|
| `PILOTHOUSE_DATADOG_API_KEY` + `_APP_KEY` + `_SITE` | live Datadog |
| `PILOTHOUSE_GITHUB_TOKEN` | live GitHub |
| `PILOTHOUSE_PAGERDUTY_TOKEN` | live PagerDuty |
| `PILOTHOUSE_SLACK_BOT_TOKEN` | live Slack + Slack 通知 |
| `PILOTHOUSE_KUBE_API_URL` + `_TOKEN` + `_CA_PATH` | live Kubernetes |
| `PILOTHOUSE_LINEAR_API_KEY` | live Linear |

### Webhook 密钥(分 source 独立轮换)

| 变量 | 校验方案 |
|---|---|
| `PILOTHOUSE_GITHUB_WEBHOOK_SECRET` | GitHub `X-Hub-Signature-256` |
| `PILOTHOUSE_SLACK_SIGNING_SECRET` | Slack v0 + 5 分钟窗 |
| `PILOTHOUSE_PAGERDUTY_WEBHOOK_SECRET` | PagerDuty 多 key `v1=…` |
| `PILOTHOUSE_DATADOG_WEBHOOK_SECRET` | Datadog `DD-Signature` |
| `PILOTHOUSE_WEBHOOK_SECRET` | 通用 HMAC-SHA256 |

### 通知

| 变量 | 用途 |
|---|---|
| `PILOTHOUSE_NOTIFY_WEBHOOK_URL` | `approval_requested` + `run_failure` 触发的外部 URL |

### Git workflow(auto-PR 模板)

| 变量 | 默认值 | 用途 |
|---|---|---|
| `PILOTHOUSE_GIT_BRANCH_PREFIX` | `pilothouse` | bot 分支名第一段 |
| `PILOTHOUSE_GIT_COMMIT_SIGNOFF` | `false` | commit 尾加 `Signed-off-by:` |
| `PILOTHOUSE_GIT_PR_DRAFT` | `false` | 以 draft 开 auto-PR |

## 10. 部署

### 方案 A —— 单进程 + SQLite(最小)

```bash
pilothouse serve     # 监听 127.0.0.1:8088
```

开发和小型单租户部署。不需要任何外部服务。存储在 `./var/pilothouse.db`。

### 方案 B —— Docker compose(api + console + Postgres)

```bash
docker compose up -d
```

三个容器互相连好。Anthropic key + connector token 通过 env 覆盖。
DB 持久化在 Docker volume 里。

### 方案 C —— Postgres + Temporal(生产)

```bash
export PILOTHOUSE_DATABASE_URL='postgresql+asyncpg://user:pw@db:5432/pilothouse'
export PILOTHOUSE_TEMPORAL_ADDRESS=temporal.svc.cluster.local:7233
export PILOTHOUSE_API_KEYS=your-admin-key

pip install 'pilothouse[temporal]'
pilothouse serve
```

workflow 跨重启幸存;多个 worker 进程可以跑同一个 Temporal 集群,
横向扩展。`pilothouse temporal status` 验证。

### 健康检查端点

| 路径 | 内容 |
|---|---|
| `GET /healthz` | liveness —— 返回 `{"ok":true}` |
| `GET /metrics` | Prometheus 抓取目标 |
| `GET /plugins/doctor` | 列出当前 misconfigured 的插件 |
| `pilothouse plugins doctor` | 同上,exit code 非零 —— 可做 CI gate |

## 11. 插件(一分钟概要)

插件是 Pilothouse 的扩展点。5 种 kind ——
**Template**、**Connector**、**Notifier**、**Trigger**、**Hook** ——
3 种发现路径:

| 路径 | 怎么用 |
|---|---|
| **In-tree** | wheel 自带(`pilothouse.plugins.builtin`) |
| **Entry point** | `pip install pilothouse-foo`(声明 `pilothouse.plugins` entry point) |
| **目录** | 丢 `*.py` 到 `$PILOTHOUSE_PLUGIN_DIR` |

CLI:

```bash
pilothouse plugins list                              # 所有发现的插件
pilothouse plugins info <name>                       # 单插件详情
pilothouse plugins enable  / disable <name>          # 持久化
pilothouse plugins config set <name> <key> <value>
pilothouse plugins doctor                            # CI gate:misconfig 时退出码非零
pilothouse plugins install <package>                 # pip install + 自动 reload
pilothouse plugins scaffold <kind> <name>            # 30 秒上手骨架
```

**想写插件?** 看 [PLUGIN_AUTHORING.zh-CN.md](./PLUGIN_AUTHORING.zh-CN.md)。

## 12. FAQ + 坑

**Q:试用要不要 Anthropic API key?**
不要。`PILOTHOUSE_ANTHROPIC_API_KEY` 为空时,所有模板跑**mock 模式**:
确定性回放 `mock_plan`,触发的是和真实模型一样的工具调用。适合 demo、
CI 和 connector 开发。

**Q:怎么提前看 agent *将要* 做什么,但不真做?**
默认 dry-run 建 Agent,触发,看 run 的事件日志。每个破坏性工具产出
`would_have_called_with: {…}` 预览,不真执行。

**Q:告警风暴期间一个 agent 触发了 50 次,怎么办?**
检查 `PILOTHOUSE_DEDUP_WINDOW_SECONDS` —— webhook 重试应该被合并。
如果你收到 50 个**不同** payload,那 source 真的发了 50 个不同告警;
调高 `PILOTHOUSE_RATE_LIMIT_PER_MINUTE` 或检查上游告警配置。

**Q:审批没人看怎么办?**
过 `PILOTHOUSE_APPROVAL_TTL_MINUTES`(默认 60)后过期。Agent 收到
结构化的"rejected: expired"结果,继续(通常干净退出)。给 Agent 设
`notify_slack_channel`,或全局设 `PILOTHOUSE_NOTIFY_WEBHOOK_URL`,
保证有人看到。

**Q:我希望某个工具 agent 不能调。**
两种办法:
1. 不暴露 —— 写一个 connector 只暴露只读路径
2. 暴露但标记 `is_destructive=True` —— 每次调用走 dry-run + 审批,
   你决定批不批

**Q:不给任何云凭证怎么跑?**
任何 `PILOTHOUSE_*_TOKEN` / `_API_KEY` 都别设。connector 自动降级到
**mock 模式**,返回确定性合成数据 —— 不碰真实服务就能驱动每个模板
端到端。

**Q:能不能多个 Pilothouse 进程对同一个数据库?**
能 —— 如果 `PILOTHOUSE_DATABASE_URL` 是 Postgres。SQLite 多进程不
推荐;dedup / rate-limit 状态是 per-process 的(跨进程 workflow 持
久化用 Temporal 模式)。

**Q:一个插件坏了,CLI 都禁不掉。**
直接改表:
```sql
UPDATE plugins SET enabled = false WHERE name = 'broken_plugin';
```
或者目录式发现的话,把文件从 `PILOTHOUSE_PLUGIN_DIR` 里删掉。

## 下一步

- **写插件** → [PLUGIN_AUTHORING.zh-CN.md](./PLUGIN_AUTHORING.zh-CN.md)
- **示例插件** → [`examples/plugins/`](../examples/plugins/)
- **项目主 README** → [`../README.md`](../README.md)(简体中文) · [`../README.en.md`](../README.en.md)(English)
