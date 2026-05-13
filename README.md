# Pilothouse

[English](./README.en.md) · **简体中文**

> **声明** —— 本项目**仅供学习与参考之用**,**不提供任何形式的技术支持**,
> 也不对正确性、安全性或生产可用性作任何保证。使用风险自担。

> AI DevOps Copilot 平台 —— 配置一次 Agent,让它在 CI/CD、监控、IaC
> 等场景下按触发器自动运行。

Pilothouse 是一个**多租户、生产级**的平台,用来把 LLM 驱动的自动化
嵌进 DevOps 工作流。运维同学注册若干 **Agent**(name + template +
params + trigger),从此这些 Agent 会在 cron / webhook / API 触发时
自动跑起来 —— 调查告警、审查 PR、诊断 pod 故障、跑 Terraform plan
review,然后把结论回写回去。每一个破坏性操作要么走 dry-run 预览,
要么走显式人工审批;每一步 LLM 调用都进入 append-only 审计日志。

```
[ webhooks ]                                         ┌──────────────┐
[ cron     ] ──► FastAPI server ──► AgentRunner ──► │ 工具注册表    │
[ CLI / UI ]      (multi-tenant)    (tool-use loop) │ • Datadog     │
                                          │         │ • GitHub      │
                                          ▼         │ • PagerDuty   │
                              SQLite / Postgres     │ • Slack       │
                              agents · runs ·       │ • Kubernetes  │
                              events · approvals ·  │ • MCP (任意)  │
                              tenants · mcp_servers └──────────────┘
                                          │
                              SSE • Prometheus • Slack/webhook 通知
                              JSON/CSV 审计导出 • cost 仪表盘
```

---

## 快速开始

```bash
# 方式 A —— 本地 Python + uv(推荐;管 Python 版本 + 依赖 + lock)
curl -LsSf https://astral.sh/uv/install.sh | sh    # 装一次
uv sync --all-extras                               # 读 .python-version(3.12)和 pyproject
uv run pilothouse db init
uv run pilothouse demo                             # 一键创建每种 Agent + 跑一次 mock
uv run pilothouse serve                            # http://127.0.0.1:8088
cd console && npm install && npm run dev           # http://localhost:3000

# 方式 B —— 本地 Python + pip(传统)
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,temporal]'
pilothouse db init && pilothouse demo

# 方式 C —— Docker 全栈(api + console + Postgres)
docker compose up
# api      http://localhost:8088
# console  http://localhost:3000
```

`pilothouse demo` 会创建每个模板对应的 Agent,然后在 mock 模式下各
触发一次,打印完整审计轨迹。**不需要任何 API key**。`uv.lock` 已经
提交进 git,所有贡献者和 CI 看到的依赖树**完全一致**。

### 文档

| 适合谁 | 文档 |
|---|---|
| **运维方** —— 跑 Pilothouse | [使用指南](./docs/USER_GUIDE.zh-CN.md) —— 安装、配置、触发、审批、部署 |
| **插件作者** —— 扩展 Pilothouse | [插件作者指南](./docs/PLUGIN_AUTHORING.zh-CN.md) —— 5 种类型、scaffold、测试、分发 |
| **CLI 速查** | `pilothouse --help`、`pilothouse <subcommand> --help` |
| **HTTP API 参考** | `pilothouse serve` 后访问 `/docs` 看自动生成的 OpenAPI |

---

## 已实现功能

### 1. 插件系统

整个平台都是插件化的。所有模板、connector、通知器、触发器和生命周期
钩子 —— 包括内置的那些 —— 都通过同一个 `PluginManager` 注册。运维
不改代码就能在运行时切换:

| | |
|---|---|
| **5 种插件类型** | `TemplatePlugin`(新 Agent 模板) · `ConnectorPlugin`(外部服务工具) · `NotifierPlugin`(事件总线订阅者) · `TriggerPlugin`(新触发源) · `HookPlugin`(`before_run` / `after_run` 生命周期切面) —— 一个插件可以多继承混合多种类型 |
| **3 种发现路径** | **In-tree builtins**(wheel 自带的 8 模板 + 6 connector) · **Entry points**(任意 pip 包的 `[project.entry-points."pilothouse.plugins"]`) · **目录扫描**(`PILOTHOUSE_PLUGIN_DIR` 或 `./plugins/` 用于本地迭代) |
| **持久化 enable/disable** | `pilothouse plugins disable builtin.template.flaky_test_hunter` 翻 `plugins` 表里的一行。重启后状态保留,关掉的插件从活跃 registry 里被摘掉 |
| **热重新发现** | `pilothouse plugins reload`(或 `POST /plugins/reload`)不重启进程就能重新扫 entry points + 目录 —— 适合刚丢进新插件文件后 |
| **声明式配置 schema** | 插件声明需要的 config 字段(支持 `secret` / `default` / `env_fallback`);运维通过 CLI / HTTP / UI 设置;manager 在激活时校验,缺必填项时把插件标记为 **misconfigured** 而不是直接崩。`pilothouse plugins doctor` 在有任何 misconfig 时退出码非零 —— CI 部署前可直接用作健康检查 |
| **内置和第三方走同一套 API** | 内置都是 `BuiltinTemplatePlugin` / `BuiltinConnectorPlugin` 适配器。组织内部把私有模板打成 pip 包,加载方式完全一样 |
| **控制台 UI** | `/plugins` 路由 —— 列表 / 按 kind / status 过滤 / enable / disable / 内联编辑配置(secret 自动 mask)。顶部横幅高亮 misconfigured 插件 |
| **作者指南 + 示例** | `examples/plugins/discord_notifier.py`(带 config schema 的 notifier) · `examples/plugins/poll_url_trigger.py`(带 start/stop 生命周期的 trigger) · `examples/plugins/README.md` |

CLI:

```bash
pilothouse plugins list                              # 发现 + 显示所有插件状态
pilothouse plugins info <name> [--reveal]            # 单插件详情(meta + schema + config)
pilothouse plugins enable  / disable <name>          # 持久化 round-trip
pilothouse plugins reload                            # 重新扫描 entry points + 目录
pilothouse plugins doctor                            # 任何 misconfig 时退出码 1
pilothouse plugins config show <name> [--reveal]    # 默认 mask
pilothouse plugins config set   <name> <key> <value>
pilothouse plugins config unset <name> <key>
pilothouse plugins scaffold <kind> <name>            # 生成插件骨架 + 测试 + pytest.ini
pilothouse plugins install <package>                 # pip install + 重新发现(entry-point 插件)
```

HTTP(仅 admin 租户):`GET /plugins` · `POST /plugins/{name}/{enable,disable}` · `POST /plugins/reload` · `GET /plugins/doctor` · `GET/POST /plugins/{name}/config`。

**想写插件?** 30 秒上手:

```bash
pilothouse plugins scaffold notifier my_discord     # 生成 plugin.py + 测试 + pytest.ini
PILOTHOUSE_PLUGIN_DIR=./plugins pilothouse plugins reload
pilothouse plugins info my_discord                  # 看需要的 config
pytest plugins/tests/                               # 异步测试开箱即用
```

完整作者指南:[`docs/PLUGIN_AUTHORING.zh-CN.md`](./docs/PLUGIN_AUTHORING.zh-CN.md)。
测试工具在 `pilothouse.testing`:
`mock_tool_context` · `make_event` · `capture_events` · `temp_plugin_manager`。

### 2. Agent 运行时

| | |
|---|---|
| **Tool-use 循环** | 基于 Anthropic 的 tool-use 协议;每轮都是检查点,审批门暂停后可在不同进程内继续 |
| **Mock 模式** | 当 `PILOTHOUSE_ANTHROPIC_API_KEY` 为空时,运行时按模板的 `mock_plan()` 确定性回放 —— 整套栈可离线跑测试和演示 |
| **可恢复状态** | `Run.state_json` 持久化循环状态(messages、待审批工具调用、已执行结果) |
| **协作式取消** | 循环每次迭代检查 `Run.status`;外部 `cancel_run` 能干净退出并保留完整审计 |
| **成本跟踪** | 每次 run 记录 `tokens_input` / `tokens_output` / `cost_usd_cents`,在 dashboard 和 Prometheus 都能看到 |

### 3. 内置模板

产品交付面 —— 8 个开箱即用的剧本:

| 模板 | 触发方式 | 做什么 |
|---|---|---|
| `datadog_alert_triage` | Datadog webhook / PagerDuty | 拉告警 + 指标 + 近期部署 + 日志 → 排序后的分诊报告 |
| `pr_security_scanner` | GitHub PR webhook | diff 扫描:secrets / 依赖升级 / IAM / migration → REQUEST_CHANGES 评论 |
| `k8s_pod_investigator` | Alertmanager | `describe pod` + events + 上一次容器日志 → 排名前 5 的可能原因 |
| `terraform_plan_reviewer` | GitHub PR webhook | 把每条变更分类为 BLOCKING / RISKY / SAFE → review 评论 |
| `pagerduty_first_responder` | PagerDuty webhook | 收集 incident 上下文,推到 Slack,写 incident note。**不 ack** —— 那是人的决定 |
| `flaky_test_hunter` | Cron(夜间) | 扫近期 CI 找 pass→fail→pass 的飘忽测试 → tracking issue 摘要 |
| `bug_auto_fixer` | Cron / Linear webhook | 拉一个带标签的 Linear 工单 → 读引用的源文件 → 按 git 约定建分支 + commit + PR → 在工单回评 |
| `pr_code_reviewer` | GitHub PR webhook | 多维度评审(正确性 / 性能 / 可读性 / 安全 / 测试)→ 一条结构化 GitHub Review,带**按行锚定的 inline 评论** |

### 4. Connectors

**live** 模式发真实 HTTP,**mock** 模式返回确定性的合成响应(没凭证
也能开发模板)。

| Connector | 工具 | live 模式 env var |
|---|---|---|
| `datadog` | query_metric, get_alert, recent_deploys, search_logs | `_DATADOG_API_KEY` + `_APP_KEY` |
| `github` | get_pr, get_pr_diff, get_pr_files, get_file_content, list_recent_commits, post_pr_comment ⚠, create_branch ⚠, create_or_update_file ⚠, create_pull_request ⚠, create_pr_review ⚠ | `_GITHUB_TOKEN` |
| `pagerduty` | get_incident, add_note ⚠, acknowledge ⚠ | `_PAGERDUTY_TOKEN` |
| `slack` | post_message ⚠ | `_SLACK_BOT_TOKEN` |
| `kubernetes` | describe_pod, get_pod_events, get_pod_logs, list_pods | `_KUBE_API_URL` + `_TOKEN` |
| `linear` | list_issues, get_issue, add_comment ⚠, update_status ⚠ | `_LINEAR_API_KEY` |
| `mcp` | _任意已注册 MCP server 暴露的工具_ | n/a |

⚠ = 破坏性(走 dry-run + 审批门)

### 5. MCP 适配器

**任意** MCP(Model Context Protocol)server 都能注册成 Pilothouse
connector —— 它的工具会和原生 connector 一起出现,享受相同的 dry-run
和审批门。

```bash
# stdio transport(uvx / npx 启动的 server)
pilothouse connectors add-mcp time uvx mcp-server-time

# HTTP transport(托管的 MCP server)
pilothouse connectors add-mcp finance --http https://mcp.example/rpc \
    --header "Authorization=Bearer $TOKEN"

# 标记特定工具为破坏性(纳入审批门)
pilothouse connectors add-mcp ops mcp-ops \
    --destructive delete_user --destructive drop_table
```

MCP server 注册信息持久化在 `mcp_servers` 表,服务启动时自动重新挂上。
当上游工具的 `inputSchema.x-destructive` 为 `true` 时,destructive 标记
会被自动识别。

### 6. 触发器

| 触发器 | 配置 |
|---|---|
| **手动(CLI)** | `pilothouse agents trigger <id> --file payload.json` |
| **手动(HTTP)** | `POST /agents/{id}/trigger` 带 `{"payload": {...}}` |
| **Cron** | 在 Agent 上设 `schedule_cron`;APScheduler 触发 |
| **Webhook(Datadog)** | `POST /webhooks/datadog/{agent_id}` —— `DD-Signature` 签名校验 |
| **Webhook(GitHub)** | `POST /webhooks/github/{agent_id}` —— `X-Hub-Signature-256` 签名校验 |
| **Webhook(PagerDuty)** | `POST /webhooks/pagerduty/{agent_id}` —— `X-PagerDuty-Signature` 多 key 支持(便于轮换) |
| **Webhook(Slack)** | `POST /webhooks/slack/{agent_id}` —— Slack v0 签名 + 5 分钟时间窗 |
| **Webhook(Alertmanager)** | `POST /webhooks/alertmanager/{agent_id}` |
| **Webhook(generic)** | `POST /webhooks/generic/{agent_id}` —— 通用 HMAC-SHA256 |

每个 source 有独立的 secret env var(`PILOTHOUSE_*_WEBHOOK_SECRET`),
方便单独轮换。secret 留空 = 该 source 跳过校验(开发友好的默认)。

### 7. 安全门

每个破坏性工具调用都过三层:

1. **`dry_run`** —— 开启时,破坏性工具不会真执行;运行时返回
   `would_have_called_with: {…}` 预览。在 runtime 层强制,即使
   connector 忘记自检也不会漏。
2. **审批门** —— 当 `dry_run=false` 且 `require_approval_for_writes=true`
   (默认)时,运行时暂停,创建 `Approval` 行(带工具输入和模型理由),
   然后退出。`resume_run` 接受运维决定后重新进入循环。
3. **审批 TTL** —— 待审批超过 `PILOTHOUSE_APPROVAL_TTL_MINUTES`
  (默认 60)的会被后台 sweeper 自动 reject,run 用结构化的"过期"拒绝
   结果给模型,继续走完。**永远不会有挂死的审批**。

审批路由:

- **Slack 原生交互式审批** —— 在 Agent 上设 `notify_slack_channel: "#sre"`
  会推送一条带 Approve / Reject 按钮的 block-kit 消息。点击后 POST 到
  `/webhooks/slack/interactivity`(签名校验)→ 解析审批 → 原地更新消息。
  **不用离开 Slack** 就能批准 PR 评论或运维步骤。
- **通用 webhook** —— 设 `PILOTHOUSE_NOTIFY_WEBHOOK_URL` 把审批扇出
  到 Opsgenie、Discord、自建路由器等。

批量操作:

```bash
pilothouse approvals approve-all --tool github_post_pr_comment --by alice
pilothouse approvals reject-all --agent scanner-foo --reason "rotate first"
```

也可以 `POST /approvals/resolve-batch` 传 ids 数组或 filter。

### 8. Run 生命周期

| | |
|---|---|
| **取消** | `POST /runs/{id}/cancel` 翻状态;循环在下次迭代边界退出。如果在审批门暂停,所有待审批会被自动 reject 并带上取消原因 |
| **重跑** | `POST /runs/{id}/retry` 用同一 trigger payload 起一个新 run |
| **搜索** | `GET /runs?status=&agent=&trigger=&q=&limit=&offset=` —— 跨 Agent 的租户级搜索 |
| **JSON 导出** | `GET /runs/{id}/export.json` —— 完整审计包(run + agent 快照 + 全部 events + 全部 approvals)。可直接贴到 SOC2 工单 |
| **CSV 导出** | `GET /runs/{id}/export.csv` —— 事件时间线扁平化 |
| **实时 SSE** | `GET /runs/{id}/events/stream` —— 先回放历史,再挂到内部 bus 实时推,terminal 后发 `event: end` 关闭 |
| **美化 CLI** | `pilothouse runs logs <id>` —— 着色、单行/事件;`--kind tool_call --tail 20` 过滤 |

### 9. 多租户

单租户安装零变化 —— 启动时自动创建 `default` 租户,旧的
`PILOTHOUSE_API_KEYS` 环境变量自动并入它的 keys 列表。

| | |
|---|---|
| **隔离** | 每个 Agent / Run / Approval / MCP server 都带 `tenant_id`。跨租户查询统一返回 **404**(不泄露资源是否存在) |
| **鉴权** | `Authorization: Bearer <key>` 或 `X-API-Key: <key>` 通过中间件解析为租户;`request.state.tenant_id` 在每个 per-resource 查询里强制过滤 |
| **同名 Agent 跨租户允许** | 名字只在租户内唯一,通过 `(tenant_id, name)` 约束实现 |
| **配额** | 每租户的 `max_agents`(超 → 403)和 `max_runs_per_day`(超 → 429),通过 `pilothouse tenants set-quota` 设置 |
| **速率限制** | 滑动 60 秒窗口,每租户独立,`PILOTHOUSE_RATE_LIMIT_PER_MINUTE` 控制 |
| **触发去重** | webhook 重试(Datadog / GitHub 在 5xx 时会重试)在 `PILOTHOUSE_DEDUP_WINDOW_SECONDS` 窗口内合并 —— 同 Agent + 同 payload 摘要返回已有 run id,不会再起一个 |
| **CLI 管理** | 所有租户 CRUD 都走 `pilothouse tenants` —— **不通过 HTTP** —— 这样泄露的租户 key 永远无法升权 |
| **级联删除** | `pilothouse tenants delete <name>` 清理它名下所有 agents/runs/approvals/MCP server;`default` 租户被保护不能删 |
| **掩码 key 列表** | `pilothouse tenants show-keys <name>` 显示 `phk_xxxx…yyyy + 长度` —— 创建后再也不会明文显示 |

```bash
pilothouse tenants create acme --display-name "Acme Corp"
pilothouse tenants add-key acme                  # 自动生成 phk_… key
pilothouse tenants set-quota acme --max-agents 10 --max-runs-per-day 500
```

### 10. Git 工作流约定(Bug → PR)

`bug_auto_fixer` 会**写你的代码仓**。为了让机器人提的历史一眼能辨认,
所有 PR 都按一套严格、可覆盖的约定走:

| | 格式 | 例子 |
|---|---|---|
| **分支** | `<prefix>/<type>/<TICKET-ID>-<slug>` | `pilothouse/fix/ENG-1234-npe-in-get-user-when-db-lookup-misses` |
| **Commit subject** | Conventional Commits `<type>(<scope>): <subject>`(≤72 字) | `fix(users): NPE in get_user when DB lookup misses` |
| **Commit body** | 可选,中英文都行的上下文说明 | "Adds an early return in `get_user` and a regression test." |
| **Commit footer** | `Closes <TICKET-ID>` (+ 可选 `Signed-off-by`) | `Closes ENG-1234` |
| **PR title** | 等同 commit subject | |
| **PR body** | 总结 + 改动文件 + 测试说明 + `Closes <TICKET-ID>` + `🤖 Opened by Pilothouse` 尾巴 | |

`<prefix>/` 让 bot 分支和人类分支视觉上分开,也方便仓库管理员写保护规则:

```yaml
# .github/branch-protection.yml(示意)
rules:
  - pattern: "pilothouse/**"
    required_reviewers: ["@sre"]
    require_signed_commits: true
```

可调节项(env 或 per-agent params):

| 设置 | 作用 | 默认值 |
|---|---|---|
| `PILOTHOUSE_GIT_BRANCH_PREFIX` / agent param `branch_prefix` | bot 分支的第一段 | `pilothouse` |
| `PILOTHOUSE_GIT_COMMIT_SIGNOFF` | 在 commit 末尾加 `Signed-off-by:`(DCO 友好) | `false` |
| `PILOTHOUSE_GIT_PR_DRAFT` / agent param `draft` | 以 draft 形式开 PR,要求人工标记 ready | `false` |

完整链路(读工单 → 读文件 → 建分支 → commit → 开 PR → 回评工单)是
一次 `bug_auto_fixer` 运行。每个 write 步骤都是**破坏性**操作,走审批门:

```bash
# 先 dry-run(默认)—— 看它会做什么但不真的做
pilothouse agents create bug-fixer bug_auto_fixer \
    --param repo='"acme/api"' --param label='"pilothouse-fix"'
echo '{"issue":{"identifier":"ENG-1234"}}' \
    | pilothouse agents trigger bug-fixer

# 切换到 live + 审批门
pilothouse agents create bug-fixer-live bug_auto_fixer \
    --param repo='"acme/api"' --param notify_slack_channel='"#sre-approvals"' \
    --no-dry-run
# → 第一个破坏性操作(create_branch)会暂停等审批;
#   在 Slack 点 Approve 后自动 resume,走完 commit + PR + 工单回评
```

### 11. PR 代码评审(inline)

`pr_code_reviewer` 接 GitHub PR webhook。它会发**一条**结构化的
GitHub Review,内含:

- 顶部 body:总评 + 按严重度分类的发现数
- inline 评论:每条都锚定到 `(path, line)`,在 PR diff 视图里**贴着代码**显示
- event:基于发现的严重度,选 `APPROVE` / `REQUEST_CHANGES` / `COMMENT`

评审维度可在 agent params 里勾选:

```bash
pilothouse agents create reviewer pr_code_reviewer \
    --param repo='"acme/api"' \
    --param dimensions='["correctness","security","tests"]' \
    --param block_on_findings=true
```

在 GitHub 仓库设置里 webhook URL 填:
`POST /webhooks/github/<agent_id>` —— 走 `X-Hub-Signature-256` 校验。

### 12. GitOps(声明式 Agent 管理)

把整个 Agent 集群定义在 `agents.yaml`,提交到 git,从 CI 驱动:

```yaml
version: 1
defaults:
  dry_run: true
prune: false           # 设 true 会删除 manifest 之外的 Agent
agents:
  - name: triage-checkout
    template: datadog_alert_triage
    description: 调查 checkout 延迟告警
    params:
      service: checkout
      slack_channel: "#sre-checkout"
      notify_slack_channel: "#sre-approvals"
  - name: nightly-flaky-scan
    template: flaky_test_hunter
    params:
      repo: acme/api
      tracking_issue: 42
    schedule_cron: "0 5 * * *"
```

```bash
pilothouse plan  -f agents.yaml                  # tf 风格的 + ~ - diff
pilothouse apply -f agents.yaml --auto-approve   # CI 友好
pilothouse export -o agents.yaml                 # 把当前状态反向导出
```

`POST /manifest/{plan,apply,export}` 在 HTTP 上暴露同样的工作流。

### 13. 可观测性

| 维度 | 内容 |
|---|---|
| **审计日志** | 每个 model turn / tool call / tool result / approval 请求/解决/过期 / decision / error 都进 append-only 的 `events` 表。回放可重建 run |
| **Prometheus `/metrics`** | counter:按 kind 分组的 events、tool 调用、审批决定、run terminal 状态。gauge:agents 数、待审批数、暂停 run 数 |
| **`/stats?days=N`** | 按天 / 按 Agent / 按 status 聚合的 runs / tokens / cost-USD。控制台 dashboard 后端 |
| **SSE 实时推送** | `/runs/{id}/events/stream`,UI 不用 polling 就能实时刷 |
| **失败通知** | Agent 上设 `notify_on_failure: "#sre"`,run 进入 `failed` 或 `cancelled` 时推 Slack;通用 webhook 也会收到 |
| **控制台 dashboard** | 每天 cost 柱状图、按 Agent 排序的 cost、按 status 分布,`1d/7d/30d` 切换 |

### 14. Web 控制台(Next.js 15)

服务端渲染,无客户端状态。11 个路由:

| 路由 | 内容 |
|---|---|
| `/` | 总览统计 + 最近 runs |
| `/dashboard` | cost & token 图表,按天/按 Agent/按 status |
| `/agents` | 列表 + 实时搜索(name / template / description)+ template 下拉过滤 |
| `/agents/new` | 创建表单(template 选择 + JSON params 编辑器) |
| `/agents/[id]` | 详情 + 手动触发面板 + 最近 runs |
| `/runs` | 跨 Agent 搜索:status / agent / trigger / summary + 分页 |
| `/runs/[id]` | run 概要 + approvals + **实时 SSE 时间线** + Cancel + Retry |
| `/approvals` | pending / approved / rejected,**复选批量** approve/reject |
| `/schedule` | cron 驱动的 Agent + 计算出的下次触发时间 |
| `/system` | 模板 + connector 的 live/mock 状态 |

导航栏从 `/me` 拉取**租户指示器**,运维永远清楚自己在操作哪个租户。

### 15. 部署

| | |
|---|---|
| **Dockerfile** | 多阶段,~150 MB 运行时镜像,非 root 用户,`/healthz` healthcheck |
| **Console Dockerfile** | 多阶段 Next.js build → 极简 Node 运行时 |
| **`docker-compose.yml`** | 一个命令拉起 api + console + Postgres;通过 env 传 Anthropic key 和其他密钥 |
| **Postgres ready** | 把 `PILOTHOUSE_DATABASE_URL` 换成 `postgresql+asyncpg://…` 即可;compose stack 自动这么做 |
| **CI workflow** | `.github/workflows/ci.yml` 每次 push 跑 ruff + pytest(mock 模式)+ Next.js build + Docker 镜像 smoke test |

---

## 配置

所有配置都是 `PILOTHOUSE_` 前缀的环境变量,通过 pydantic-settings 加载,
也支持 `.env` 文件。

### 核心

| 变量 | 用途 | 默认值 |
|---|---|---|
| `PILOTHOUSE_ANTHROPIC_API_KEY` | 设了就用真实 Claude,否则 mock | `""` |
| `PILOTHOUSE_MODEL_PLANNER` | planner 模型 id | `claude-opus-4-5` |
| `PILOTHOUSE_MODEL_WORKER` | 高频小任务的模型 id | `claude-haiku-4-5` |
| `PILOTHOUSE_DATABASE_URL` | SQLAlchemy URL | `./var` 下的 sqlite |
| `PILOTHOUSE_DATA_DIR` | SQLite + state 文件目录 | `./var` |
| `PILOTHOUSE_DRY_RUN_DEFAULT` | 新 Agent 默认 dry-run | `true` |
| `PILOTHOUSE_HOST` / `PILOTHOUSE_PORT` | HTTP 服务监听 | `127.0.0.1` / `8088` |
| `PILOTHOUSE_MAX_TOOL_ITERATIONS` | 单个 run 的最大循环次数;超过即 failed | `12` |
| `PILOTHOUSE_MAX_OUTPUT_TOKENS` | 单轮 LLM 输出上限 | `4096` |

### 鉴权 + 安全

| 变量 | 用途 | 默认值 |
|---|---|---|
| `PILOTHOUSE_REQUIRE_APPROVAL_FOR_WRITES` | 破坏性工具走审批门 | `true` |
| `PILOTHOUSE_API_KEYS` | 旧版:逗号分隔,bootstrap 时灌入 default 租户 | `""` |
| `PILOTHOUSE_APPROVAL_TTL_MINUTES` | 自动拒绝超期待审批 | `60` |
| `PILOTHOUSE_APPROVAL_SWEEP_INTERVAL_SECONDS` | sweeper 运行间隔 | `30` |
| `PILOTHOUSE_DEDUP_WINDOW_SECONDS` | 触发去重窗口(0 关闭) | `60` |
| `PILOTHOUSE_RATE_LIMIT_PER_MINUTE` | 每租户触发上限(0 关闭) | `60` |
| `PILOTHOUSE_METRICS_ENABLED` | 暴露 `/metrics` | `true` |
| `PILOTHOUSE_GIT_BRANCH_PREFIX` | auto-PR 分支的第一段 | `pilothouse` |
| `PILOTHOUSE_GIT_COMMIT_SIGNOFF` | auto-commit 末尾加 `Signed-off-by:` | `false` |
| `PILOTHOUSE_GIT_PR_DRAFT` | 以 draft 形式开 auto-PR | `false` |

### Connector 凭证

| 变量 | 启用 |
|---|---|
| `PILOTHOUSE_DATADOG_API_KEY` + `_APP_KEY` + `_SITE` | live Datadog |
| `PILOTHOUSE_GITHUB_TOKEN` | live GitHub |
| `PILOTHOUSE_PAGERDUTY_TOKEN` | live PagerDuty |
| `PILOTHOUSE_SLACK_BOT_TOKEN` | live Slack + Slack 通知 |
| `PILOTHOUSE_KUBE_API_URL` + `_TOKEN` + `_CA_PATH` | live Kubernetes |
| `PILOTHOUSE_LINEAR_API_KEY` | live Linear(`bug_auto_fixer` 用) |

### Webhook 密钥(每个 source 一个,方便独立轮换)

| 变量 | 校验方案 |
|---|---|
| `PILOTHOUSE_GITHUB_WEBHOOK_SECRET` | GitHub `X-Hub-Signature-256` |
| `PILOTHOUSE_SLACK_SIGNING_SECRET` | Slack v0 + 5 分钟时间窗 |
| `PILOTHOUSE_PAGERDUTY_WEBHOOK_SECRET` | PagerDuty 多 key `v1=…` |
| `PILOTHOUSE_DATADOG_WEBHOOK_SECRET` | Datadog `DD-Signature` |
| `PILOTHOUSE_WEBHOOK_SECRET` | 通用 HMAC-SHA256(alertmanager + generic) |

### 通知

| 变量 | 用途 |
|---|---|
| `PILOTHOUSE_NOTIFY_WEBHOOK_URL` | `approval_requested` 和 `run_failure` 时调用的外部 URL |

---

## CLI 参考

```
# 服务 / 数据库
pilothouse serve                              # HTTP + scheduler + notifier
pilothouse db init                            # 建表 + 启动 default 租户

# 发现
pilothouse templates list
pilothouse connectors list
pilothouse connectors add-mcp NAME CMD ARG...        # stdio MCP server
pilothouse connectors add-mcp NAME --http URL \      # HTTP MCP server
    --header "Authorization=Bearer xxx" --destructive delete_user
pilothouse connectors remove-mcp NAME

# Agents
pilothouse agents create NAME TEMPLATE \
       --param service=checkout \
       --param slack_channel='"#oncall"'      # 值会按 JSON 解析
pilothouse agents list
pilothouse agents show <id-or-name>
pilothouse agents trigger <id> --file event.json
pilothouse agents delete <id>

# Runs
pilothouse runs show <run-id>
pilothouse runs logs <run-id>                 # 着色时间线
pilothouse runs cancel <run-id> --by alice
pilothouse runs retry <run-id>

# 审批
pilothouse approvals list                     # 默认显示 pending
pilothouse approvals show <approval-id>
pilothouse approvals approve <approval-id> --by alice
pilothouse approvals reject <approval-id> --by alice --reason "rotate first"
pilothouse approvals approve-all --tool github_post_pr_comment --by alice
pilothouse approvals reject-all --agent scanner-foo --by alice --reason "..."
pilothouse sweep-approvals                    # 一次性 TTL 清扫

# 租户
pilothouse tenants list
pilothouse tenants create acme --display-name "Acme Corp"
pilothouse tenants add-key acme               # 自动生成 phk_… key
pilothouse tenants add-key acme --key existing-key
pilothouse tenants remove-key acme <key>
pilothouse tenants set-quota acme --max-agents 10 --max-runs-per-day 500
pilothouse tenants show-keys acme             # 掩码显示
pilothouse tenants delete acme

# GitOps
pilothouse plan -f agents.yaml
pilothouse apply -f agents.yaml --auto-approve
pilothouse export -o agents.yaml

# 杂项
pilothouse demo                               # bootstrap 每种 Agent 各跑一次
```

---

## HTTP API

OpenAPI 自动文档在 `/docs`,ReDoc 在 `/redoc`。Endpoint 按 tag 分组:
`meta` / `agents` / `runs` / `approvals` / `manifest` / `stats` /
`schedule` / `webhooks` / `metrics`。

### meta

| method | path | 说明 |
|---|---|---|
| GET | `/healthz` | liveness |
| GET | `/me` | 入站 API key 解析到的租户 |
| GET | `/templates` | 模板列表 |
| GET | `/connectors` | connector 列表 + live/mock 状态 |

### agents

| method | path | 说明 |
|---|---|---|
| POST | `/agents` | 创建 |
| GET | `/agents` | 列表 |
| GET | `/agents/{id}` | 单个 |
| PATCH | `/agents/{id}` | 更新 |
| DELETE | `/agents/{id}` | 删除 |
| POST | `/agents/{id}/trigger` | 手动触发,返回新 Run |
| GET | `/agents/{id}/runs` | 最近 runs |

### runs

| method | path | 说明 |
|---|---|---|
| GET | `/runs` | 租户级跨 Agent 搜索:`?status=&agent=&trigger=&q=&limit=&offset=` |
| GET | `/runs/{id}` | run 概要 |
| GET | `/runs/{id}/events` | 完整审计日志 |
| GET | `/runs/{id}/events/stream` | SSE:先回放再实时(terminal 后 `event: end` 关闭) |
| GET | `/runs/{id}/export.json` | 完整审计包(run + agent + events + approvals) |
| GET | `/runs/{id}/export.csv` | 事件时间线 CSV |
| POST | `/runs/{id}/cancel` | `{"by":"…"}` —— 协作式取消 |
| POST | `/runs/{id}/retry` | 用同一 payload 重跑 |
| GET | `/runs/{id}/approvals` | 这个 run 的审批 |

### approvals

| method | path | 说明 |
|---|---|---|
| GET | `/approvals` | 过滤 `?status=&tool=&agent=` |
| GET | `/approvals/{id}` | 单个 |
| POST | `/approvals/{id}/resolve` | `{"decision":"approve\|reject", "resolved_by", "reason"}` —— 最后一个解决后自动 resume run |
| POST | `/approvals/resolve-batch` | 批量:ids 数组或 `filters: {tool?, agent?}` |

### manifest / stats / schedule / metrics

| method | path | 说明 |
|---|---|---|
| POST | `/manifest/plan` | 算 manifest 的 diff |
| POST | `/manifest/apply` | plan + 持久化 |
| GET | `/manifest/export` | 把当前状态导出成 manifest |
| GET | `/stats?days=N` | 聚合 runs / tokens / cost |
| GET | `/schedule` | 调度的 Agent + 下次触发时间 |
| GET | `/metrics` | Prometheus 文本格式 |

### webhooks

| method | path | 说明 |
|---|---|---|
| POST | `/webhooks/datadog/{agent_id}` | 按 source 校验签名 |
| POST | `/webhooks/github/{agent_id}` | `X-Hub-Signature-256` |
| POST | `/webhooks/pagerduty/{agent_id}` | 多 key `v1=…` |
| POST | `/webhooks/slack/{agent_id}` | Slack v0 |
| POST | `/webhooks/alertmanager/{agent_id}` | 通用 HMAC |
| POST | `/webhooks/generic/{agent_id}` | 通用 HMAC |
| POST | `/webhooks/slack/interactivity` | Slack 交互组件回调(Approve / Reject 按钮) |

---

## 测试

```bash
uv sync --all-extras        # 或 `pip install -e ".[dev]"`
uv run pytest -q            # 或在已激活的 venv 里直接 `pytest -q`
```

测试套件(150+ 用例)完全在 mock 模式下跑 —— 不需要 Anthropic key,
不打外部 HTTP。CI 每次 push 都跑同一套。

---

## 执行后端

Pilothouse 根据 `PILOTHOUSE_TEMPORAL_ADDRESS` 在三种执行后端之间自动切换。
公开的 orchestration API(`execute_agent` / `resume_run` /
`cancel_run` / `retry_run`)三种后端完全一致:

| 模式 | env 值 | 提供什么 |
|---|---|---|
| **in-process** | _未设置_(默认) | 进程内 asyncio。零外部依赖 |
| **Temporal dev** | `dev` | 进程内 Temporal dev server —— **单机就能拿到 durable workflow**,不需要部署 Temporal 集群 |
| **Temporal 集群** | `host:7233` | 连真实 Temporal 集群。workflow 跨进程重启幸存,worker 可横向扩展 |

Temporal 模式把每个 Run 包装成 `PilothouseAgentRun` workflow:

```
client.start_workflow(PilothouseAgentRun.run, payload)
  → run_agent_activity      # 代理给 AgentRunner.start
  → wait_condition          # 等 approval_resolved 信号
  → resume_run_activity     # 代理给 AgentRunner.resume
  → return run_id
```

取消走 workflow signal,审批解决也走 signal。除 orchestration 层外的
代码(templates / connectors / plugins / 控制台 / CLI)完全不知道
Temporal 的存在。

`temporalio` 是**可选依赖**:

```bash
# uv
uv sync                                      # 只用 in-process
uv sync --extra temporal                     # 同时支持 dev / 集群模式

# pip
pip install pilothouse                       # 只用 in-process
pip install 'pilothouse[temporal]'           # 同时支持 dev / 集群模式
```

`pilothouse temporal status` 查看当前激活的模式。

## Roadmap(尚未实现)

- **租户内 RBAC** —— 当前是平面(同一租户的 key 能做该租户的任何事)
- **SSO / OAuth2 / SAML** —— 控制台登录
- **沙箱化代码执行** —— 给需要本地跑 terraform/opentofu 的 IaC plan/diff
  Agent 准备的
- **跨 run 的对话记忆** —— 长跑事故分诊 Agent 能记得"上一小时已经看过这个"
- **Helm chart** —— Kubernetes 部署
- **Python + TypeScript SDK** —— 作为单独发布的包

---

## 许可证

Apache-2.0.
