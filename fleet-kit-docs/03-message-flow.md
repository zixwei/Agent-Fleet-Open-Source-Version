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

编排方用 **自己的 Agent Key**（MCP 工具 `create_task`）；管理员用租户 Key 也可以建：

```http
POST /tasks
X-API-Key: <orchestrator-agent-key>
{"agent_id":"worker","title":"…"}
```

结果：任务 `submitted`，绑定到本租户与执行 Agent，并记录 `created_by_agent_id`（用租户 Key 创建时为 `null`）。返回 `task_id`——之后所有消息都挂在这个 id 上。

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

谁能改：

| 凭证 | 允许的目标状态 |
| --- | --- |
| 任务的执行 Agent（`agent_id`） | `working` / `completed` / `failed` / `cancelled` |
| 任务的创建 Agent（`created_by_agent_id`） | 只能 `cancelled` |
| 同租户其它 Agent | 无（`403`） |
| 租户管理员 Key | 任意合法迁移（运维兜底） |

实现用 **CAS**：`UPDATE ... WHERE status = 旧状态`。两个并发请求不能都从同一状态成功走出不同结果；输掉竞争的一方得到 **`409`**，应先 `get_task` 重读再决定是否重试。非法迁移仍是 `400`。

## 阶段 6：审计

每次关键写操作追加 hash 链。`GET /audit/verify` 默认从 `GENESIS` 起 **校验整条链**（分批读取，条数不限），任何一条被改都会报 `broken_at_seq`。返回的 `head_seq` / `head_hash` 是检查点：

- 增量校验：`GET /audit/verify?after_seq=<head_seq>&anchor_hash=<head_hash>`。锚点必须由调用方提供（取自上一次成功校验），**不从日志里读**，否则被改写的日志可以自证清白。
- 检查点要存到 Hub 写不到的地方。hash 链本身发现不了「最新几条被整段删掉」；拿保存的检查点去比，才能发现（日志比检查点短 → `ok: false`）。

### 关联（T09）

- 每个任务创建时生成 `trace_id`；任务上的消息默认沿用它（调用方只在传播外部链路时才自带 `trace_id`）。回复时用 `correlation_id` 填被回复的 `message_id`。
- 消息表持久化 `trace_id`、`correlation_id`、`context_id`、`protocol`、`protocol_version`。
- 审计 `actor` 仍是租户（`GET /audit` 按它隔离）；真正动手的凭证记在 `detail.principal = {kind, agent_id}`。消息、回执审计都带 `message_id`、`trace_id`；状态变更记 `from` / `to`。这些字段都在被 hash 的 `detail` 里。
- 被拒的请求（`401/403/404/409`）记为 `request.denied`，记在调用方租户名下；被拒的 Agent Key 记在它所属的租户名下（`principal.kind = refused_agent_key`）；未认证的记在 `unauthenticated` 名下。**提交的 Key 本身不落库。** 不存在的路由不记。
- `GET /tasks/{task_id}/correlation`：逐条核对该任务的消息、回执、当前状态能否对上审计记录，返回 `messages_attributed / messages_total`、`receipts_audited / receipts_total` 和 `problems` 清单；全部对上才 `ok: true`。

## 时序简图

```
Admin          Orchestrator           Hub              Worker
  |                 |                  |                  |
  |--register/approve----------------->|                  |
  |                 |--create_task---->|                  |
  |                 |--assign msg----->|                  |
  |                 |                  |<--receipt--------|
  |                 |                  |<--status working-|
  |                 |                  |<--result msg-----|
  |                 |<--receipt--------|                  |
  |                 |                  |<--status completed
  |--audit/verify--------------------->|                  |
```
