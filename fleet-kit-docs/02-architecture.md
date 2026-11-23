# 02 · 架构：组件与边界

## 逻辑组件

```
┌─────────────────────────────────────────────────────────────┐
│  MCP 宿主（Cursor / Claude Code / 其它） 或 demo 脚本进程      │
│  身份：FLEET_AGENT_API_KEY + 可选 FLEET_AGENT_ID               │
└──────────────────────────┬──────────────────────────────────┘
                           │ MCP stdio（工具调用）
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  mcp_server.py（门面）                                         │
│  六个工具 → httpx → Hub HTTP                                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ HTTP + Header X-API-Key
                           ▼
┌─────────────────────────────────────────────────────────────┐
│  Hub（FastAPI app.main）                                       │
│  registry · orchestration · messaging.bus · identity · audit │
└──────────────────────────┬──────────────────────────────────┘
                           │
                           ▼
                    SQLite（fleet.db）
              agents / tasks / messages /
              message_receipts / audit_log
```

## 进程边界

| 进程 | 职责 | 持久化 |
| --- | --- | --- |
| `uvicorn app.main:app` | 唯一权威账本 | SQLite |
| `mcp_server.py`（可多个） | 无状态适配器；每个宿主一份配置 = 一个身份 | 无 |
| `demo_two_agents.py` | 无模型绿路径：HTTP 直打 Hub | 无 |
| `mcp_http_parity.py` | 证明 MCP 工具函数与 Hub 同任务 | 无 |

**重要：** Agent 之间 **不直连**。所有协作状态以 Hub 为准。宿主 A 看不见宿主 B，除非 B 的写入已进 Hub，且 A 再读。

## 包内模块（`fleet-kit/app/`）

| 包 | 职责 |
| --- | --- |
| `identity` | 解析 `X-API-Key` → 租户 / Agent；`require_tenant_auth` / `require_agent_auth` |
| `registry` | Agent 注册、审批、挂起（**按 tenant_id 隔离**） |
| `orchestration` | 任务创建与状态机（**CAS 更新**） |
| `messaging` | 信封 `protocol=fleet`；`bus` 幂等写入与回执 |
| `storage` | SQLite；审计链原子追加 |
| `observability` | 审计记录与 `verify_chain` |

## 部署形态（1.0）

- 默认单机：SQLite + 进程内总线  
- `docker-compose` / Postgres / Valkey：**不在 1.0 演示路径**（避免「看起来接了其实没接线」）

## 版本字符串

Hub `/health` 返回 `version: 1.0.0-fleet-kit`，便于区分旧 prototype。
