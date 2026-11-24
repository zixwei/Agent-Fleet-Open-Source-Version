# 04 · 身份与租户

## 两种凭证

全部走 HTTP 头：`X-API-Key: …`

| 种类 | 环境变量 | 解析结果 | 典型用途 |
| --- | --- | --- | --- |
| 租户 Key | `AI_FLEET_API_KEY` 或 `AI_FLEET_TENANT_API_KEYS` JSON | `{tenant_id, agent_id: null}` | 注册/审批/挂起、运维兜底改状态、列审计 |
| Agent Key | `AI_FLEET_AGENT_API_KEYS` JSON | `{tenant_id, agent_id}` | 建任务、发消息、回执、推进自己被指派的任务 |

| 运维 Key（可选） | `AI_FLEET_OPERATOR_API_KEY` | `{tenant_id: "sys:operator"}` | 只能调 `/ops/*`：跨租户审计视图、完整链校验 |

运维 Key 至少 24 个字符，且不能和任何租户 Key、Agent Key 相同，否则 `/ops/*` 全部关闭、启动检查报错；它过不了租户或 Agent 接口，租户和 Agent Key 也过不了 `/ops/*`。比较用常数时间。

**租户 id 格式**：`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，不能含 `:`。`sys:` 前缀留给系统（`sys:unauthenticated`、`sys:operator`）。格式不对的 Key 一律 `401`，启动时 `validate_key_config()` 报错拒绝启动（报错只写租户 id，不写 Key）。

Agent Key 仅在对应 Agent **在该 Key 所属租户内已注册且 Active** 时有效；Key 里写的 Agent 不在本租户 → `401`。

**租户 Key 不进任何 MCP 宿主配置。** 宿主只放自己的 Agent Key（见 `mcp-host.example.json`）；`mcp_server.py` 发现 `FLEET_API_KEY` 会忽略并提示。任务状态的权限见 [03 阶段 5](03-message-flow.md)。

## 管理员门禁

`require_tenant_auth`：带 Agent 身份的 Key 调管理接口 → **403**。  
这样「被盗的 Worker Key」不能挂起同租户其它 Agent，也不能注册新 Agent。

## 租户范围

Agent 表含 `tenant_id`。审批/挂起必须 `agent_id + tenant_id` 匹配，否则 **404**（不泄露他租户是否存在该 id）。

任务始终带 `tenant_id`；跨租户 `GET /tasks/{id}` → **404**。

消息收发双方必须是 **同一租户** 下的已注册 Agent。

## 原则（继承白皮书）

- 不共享原始模型账号凭据；Agent 间只共享任务上下文  
- 身份以凭证为准，不以消息正文「我自称是谁」为准  
- 模型 API 能通 ≠ Agent 协作已通
