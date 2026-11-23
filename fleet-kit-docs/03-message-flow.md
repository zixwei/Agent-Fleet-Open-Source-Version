# 03 · 消息流：一次协作怎么走完

下面按「编排方 orchestrator + 执行方 worker」描述。两边可以是两个 MCP 宿主，也可以是 `demo_two_agents.py` 里的两个 Agent Key。

## 阶段 0：准备身份

1. **租户管理员**用租户 Key（`FLEET_API_KEY` / `AI_FLEET_API_KEY`）调用：
   - `POST /agents` 注册 `orchestrator`、`worker`（初始 `Pending`）
   - `POST /agents/{id}/approve` → `Active`
2. 环境变量配置 Agent Key 映射，例如：
   - `fleet-demo-orch` → `{agent_id: orchestrator, tenant_id: demo-tenant}`
   - `fleet-demo-worker` → `{agent_id: worker, tenant_id: demo-tenant}`
3. Agent 被挂起后，其 Key **立即失效**（`401 invalid agent credential`）。

只有 **租户管理员 Key** 能注册/审批/挂起。单个 Agent Key 调这些接口会 `403`。

## 阶段 1：创建任务

管理员（或持有租户 Key 的 MCP 工具 `create_task`）：

```http
POST /tasks
X-API-Key: <tenant-key>
{"agent_id":"worker","title":"…"}
```

结果：任务 `submitted`，绑定到本租户与执行 Agent。返回 `task_id`——之后所有消息都挂在这个 id 上。

## 阶段 2：编排方下发

编排方用 **自己的 Agent Key**：

```http
POST /tasks/{task_id}/messages
X-API-Key: <orchestrator-agent-key>
{
  "sender_agent_id": "orchestrator",
  "recipient_agent_id": "worker",
  "event_type": "task.assign",
  "payload": {"instruction": "…"},
  "idempotency_key": "可选-客户端稳定键"
}
```

Hub 行为：

1. 校验任务属于该租户（否则 `404`）
2. 若是 Agent Key，强制 `sender_agent_id` 与认证身份一致（否则 `403`）
3. 校验发送方/接收方都是本租户已注册 Agent
4. `build_envelope`：`protocol=fleet`，生成 `message_id` / `idempotency_key`
5. 按 `idempotency_key` 插入；重复则 `stored: false`，返回已存消息

## 阶段 3：执行方回执（真正「收到了」）

存储消息 ≠ 对方读到。**回执**才是交付证明：

```http
POST /tasks/{task_id}/messages/{message_id}/receipt
X-API-Key: <worker-agent-key>
{}
```

规则：

- 必须用 **Agent Key**（租户 Key → `401`）
- 必须是该消息的 `recipient_agent_id`（否则 `403`）
- 重复回执 → `stored: false`，不写第二条业务效果
- 审计记 `message.receipt` 或 `message.receipt.duplicate`

## 阶段 4：执行方回消息

Worker 再 `POST .../messages`，`sender=worker`，`recipient=orchestrator`，`event_type` 如 `task.result`。编排方再用自己的 Key 做 receipt。

## 阶段 5：状态机

```http
POST /tasks/{task_id}/status
{"status":"working"}   # submitted → working
{"status":"completed"} # working → completed
```

合法边：

- `submitted` → `working` | `cancelled`
- `working` → `completed` | `failed` | `cancelled`
- 终态不可再迁

实现用 **CAS**：`UPDATE ... WHERE status = 旧状态`。两个并发请求不能都从 `submitted` 成功走出不同终态而不冲突。

## 阶段 6：审计

每次关键写操作追加 hash 链。`GET /audit/verify` 在连续窗口上重算；窗口首条锚定其记录的 `prev_hash`（可能是 `GENESIS` 或窗外前驱），避免「只取最新 1000 条却从 GENESIS 验」的假失败。

## 时序简图

```
Admin          Orchestrator           Hub              Worker
  |                 |                  |                  |
  |--register/approve----------------->|                  |
  |--create_task---------------------->|                  |
  |                 |--assign msg----->|                  |
  |                 |                  |<--receipt--------|
  |                 |                  |<--result msg-----|
  |                 |<--receipt--------|                  |
  |--status completed----------------->|                  |
  |--audit/verify--------------------->|                  |
```
