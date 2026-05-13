# 插件作者指南

[English](./PLUGIN_AUTHORING.md) · **简体中文**

写、测、发布一个 Pilothouse 插件的端到端指南。从没写过就按顺序读;
要找特定主题直接跳。

**用** Pilothouse(而不是扩展它)看 [USER_GUIDE.zh-CN.md](./USER_GUIDE.zh-CN.md)。

## 1. 五种插件类型

| 类型 | 父类 | 什么时候用 |
|---|---|---|
| `TemplatePlugin` | 贡献 1+ 个 `Template` | 新的 Agent 剧本(比如自定义分诊工作流) |
| `ConnectorPlugin` | 贡献 1+ 个 `Connector` | 新的外部服务工具(Jira、Discord webhook、Notion 等) |
| `NotifierPlugin` | `matches()` + `dispatch()` | 订阅事件总线,推到自己的频道 |
| `TriggerPlugin` | `start()` + `stop()` 生命周期 | 新触发源(Kafka 消费、文件监听等) |
| `HookPlugin` | `before_run` / `after_run` | 审计转发、自定义 metrics、租户级账单 |

一个插件可以多继承混合多种类型。比如一个 "GitHub Pro" 插件同时贡献
新的 GitHub 工具**和**用这些工具的模板。

## 2. 30 秒上手

```bash
# 生成一个可跑的骨架 + 单测
pilothouse plugins scaffold notifier my_discord

# 发现它(没在 PILOTHOUSE_PLUGIN_DIR 里就先丢进去)
PILOTHOUSE_PLUGIN_DIR=./plugins pilothouse plugins reload
pilothouse plugins info my_discord

# 配置必填项
pilothouse plugins config set my_discord target https://discord.com/...
pilothouse plugins doctor    # 退出码 0 = 健康

# 跑它的测试
pytest plugins/tests/test_my_discord.py
```

这就是完整循环。scaffold 出来的不是 "TODO TODO TODO" 大纲,而是
**真能跑**的代码,直接可以开始编辑。

## 3. 声明式 config schema

每个插件都可以声明自己需要的配置:

```python
from pilothouse.plugins import ConfigField, NotifierPlugin, PluginMeta

class MyPlugin(NotifierPlugin):
    name = "my_plugin"

    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="webhook_url",
                description="发到哪里",
                required=True,            # 缺失 → 插件被标 misconfigured
                secret=True,              # UI/CLI 里 mask 显示
                env_fallback="MY_PLUGIN_URL",  # 不需要 PILOTHOUSE_ 前缀
            ),
            ConfigField(
                name="prefix",
                default="[bot]",
                description="每条消息加前缀",
            ),
        ]

    async def configure(self, config: dict) -> None:
        # schema 解析完后、on_load 之前调一次
        self._url = config["webhook_url"]
        self._prefix = config["prefix"]
```

激活时解析顺序:
1. 运维设置的值(`pilothouse plugins config set …` / `POST /plugins/{name}/config`)
2. `env_fallback` 环境变量
3. `default`

required 字段解析后仍为空 → 插件标为 **misconfigured** —— 在
`pilothouse plugins doctor` 和 `/plugins` 控制台页面显示。插件行还在
列表里(让运维看到坏的是哪个),但从活跃 registry 里被摘掉,保证
"禁用的工具不会被调用"。

## 4. 写单元测试

`pilothouse.testing` 提供了写插件测试需要的一切:

```python
from pilothouse.testing import make_event, mock_tool_context, capture_events
from pilothouse.events import get_bus

# Notifier 测试:
async def test_notifier_matches_approval_events():
    p = MyPlugin()
    await p.configure({"webhook_url": "https://x", "prefix": "[bot]"})
    assert p.matches(make_event("approval_requested"))
    assert not p.matches(make_event("tool_call"))

# Connector 测试:
async def test_destructive_tool_short_circuits_in_dry_run():
    conn = MyConnectorPlugin().connectors()[0]
    tool = next(t for t in conn.tools() if t.name == "my_delete_thing")
    res = await tool.handler(mock_tool_context(dry_run=True), {"id": "abc"})
    assert res.content["dry_run"] is True

# 事件总线集成测试:
async def test_event_capture():
    with capture_events() as events:
        get_bus().publish(make_event("custom", data={"x": 1}))
    assert events[0].kind == "custom"
```

完整集成测试(manager + DB + bus 都接好)用 `temp_plugin_manager`
异步上下文:

```python
from pilothouse.testing import temp_plugin_manager

async def test_my_plugin_in_isolation():
    async with temp_plugin_manager(MyPlugin()) as mgr:
        # 插件已注册、激活、configure 过。用 mgr 查 listing / doctor /
        # config;用全局 bus / registries 验证插件正确集成。
        assert "my_plugin" in {p["name"] for p in mgr.list_plugins()}
```

## 5. 三种分发路径

### A. 目录式(开发 / 私有)

```bash
cp my_plugin.py $PILOTHOUSE_PLUGIN_DIR/
pilothouse plugins reload
```

发现机制把文件用新的模块名 import,只挑出**在这个文件里定义的**
非抽象 `Plugin` 子类(import 进来的基类被排除)。

### B. Pip 包 + entry point(分享)

```toml
# 你的插件包的 pyproject.toml
[project]
name = "pilothouse-discord"
version = "0.1.0"

[project.entry-points."pilothouse.plugins"]
discord = "pilothouse_discord:DiscordNotifierPlugin"
```

`pilothouse plugins install pilothouse-discord`(或 `pip install`)
后,通过 entry-point 机制自动发现。CLI 的 `install` 命令把 pip +
reload 合成一步:

```bash
pilothouse plugins install pilothouse-discord
pilothouse plugins install ./my-plugin                    # 本地 sdist
pilothouse plugins install 'git+https://github.com/foo/bar'  # vcs
```

### C. 内置(贡献回主仓)

提一个 PR 加 `examples/plugins/…` 文件,或者(给内置的)
`pilothouse/plugins/builtin.py`。Plugin API 完全一样,差别只是代码住
哪儿。

## 6. Cookbook:常见模式

### 按租户路由

```python
class TenantAwareNotifier(NotifierPlugin):
    def matches(self, event):
        # 只处理这个租户的事件
        return event.data.get("tenant_id") == self._target_tenant
```

### 去重

```python
class DedupedNotifier(NotifierPlugin):
    def __init__(self):
        self._recently_seen: set[str] = set()

    def matches(self, event):
        key = f"{event.kind}:{event.data.get('tool', '')}:{event.run_id[:8]}"
        if key in self._recently_seen:
            return False
        self._recently_seen.add(key)
        return True
```

### 审计转发 hook

最简版本 —— 但**生产不应该这么写**:

```python
class AuditForwarder(HookPlugin):
    async def after_run(self, *, run_id, agent_id, tenant_id, status, summary):
        async with httpx.AsyncClient() as client:
            await client.post(self._target, json={
                "run_id": run_id, "agent_id": agent_id, "tenant_id": tenant_id,
                "status": status, "summary": summary[:1000],
            })
```

Hook 跑在 orchestration 链路上,慢 HTTP 目的地会卡住 agent。生产参考
实现见 [`examples/plugins/siem_audit_forwarder.py`](../examples/plugins/siem_audit_forwarder.py):
有界内存队列 + 后台 drainer + drop-oldest 背压 —— SIEM 慢了永远不会
影响 Pilothouse 延迟。

### 长轮询 trigger

```python
class KafkaTrigger(TriggerPlugin):
    async def start(self):
        self._task = asyncio.create_task(self._consume())

    async def stop(self):
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

    async def _consume(self):
        from pilothouse.orchestration import execute_agent
        async for msg in self._kafka.subscribe(self._topic):
            await execute_agent(
                agent_id=self._agent_id,
                trigger=f"plugin:{self.name}",
                trigger_payload=msg.value,
            )
```

## 7. 插件和 Temporal executor

`PILOTHOUSE_TEMPORAL_ADDRESS` 一设,agent 执行就进 Temporal workflow。
**大多数插件感知不到**:

| 插件类型 | 受 Temporal 模式影响吗? |
|---|---|
| `TemplatePlugin` | 不受影响 —— 模板由 runtime 解释,runtime 被包成 activity |
| `ConnectorPlugin` | 不受影响 —— 工具调用和 runtime 同一个 activity |
| `NotifierPlugin` | 不受影响 —— 事件总线跨 executor 共享 |
| `HookPlugin` | 不受影响 —— hook 从 orchestration 层触发,两种 executor 都一样 |
| `TriggerPlugin` | **受影响** —— `start()` 里要调 `pilothouse.orchestration.execute_agent`,这个会按当前 executor 派发。你的代码不变,新 run 自动变成 Temporal workflow |

一句话:**插件写一次,部署时是 in-process 还是 Temporal 自动切换**。
不需要任何条件代码。

## 8. 运维注意

| 关注点 | 指导 |
|---|---|
| **dispatch 里报错** | manager 会兜底 catch + log;你的 `dispatch` / `handler` 做点防御 OK,但不需要顶层 try/except |
| **慢下游** | hook / notifier 里做慢 HTTP 会卡住 orchestration 链路。用有界队列 + 后台 drainer —— 模式见 [`siem_audit_forwarder.py`](../examples/plugins/siem_audit_forwarder.py) |
| **静态存储** | 明文存 `plugin_configs.value`。生产环境用 volume / DB 层加密。UI/CLI 默认 mask |
| **兼容性** | `PluginMeta.pilothouse_compat` 目前是信息性的;未来大版本会拒绝加载不兼容 specifier |
| **有状态插件** | 在 `configure()` 里挂 `self`。manager 一个进程内只构造插件一次;多次 `enable/disable` 走 `on_load` / `on_unload` 但不重新实例化 |
| **线程** | 全 asyncio。别在 handler 里阻塞(`time.sleep` 不行,同步 HTTP 不行)。用 `httpx.AsyncClient` |
| **CI gate** | `pilothouse plugins doctor` 在有 misconfig 时退出码非零。放到部署前检查里 |

## 9. 参考

- 基类:`pilothouse.plugins.{Plugin, TemplatePlugin, ConnectorPlugin, NotifierPlugin, TriggerPlugin, HookPlugin, ConfigField, PluginMeta}`
- 测试工具:`pilothouse.testing.{mock_tool_context, make_event, capture_events, temp_plugin_manager}`
- Manager(很少直接用):`pilothouse.plugins.PluginManager`、`pilothouse.plugins.get_manager()`
- 示例插件:
  - [`discord_notifier.py`](../examples/plugins/discord_notifier.py) —— NotifierPlugin 带 config schema
  - [`poll_url_trigger.py`](../examples/plugins/poll_url_trigger.py) —— TriggerPlugin 带 start/stop 生命周期
  - [`siem_audit_forwarder.py`](../examples/plugins/siem_audit_forwarder.py) —— HookPlugin 带有界队列背压
