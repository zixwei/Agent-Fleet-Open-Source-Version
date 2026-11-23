# 04 · 身份与租户

## 两种凭证

全部走 HTTP 头：`X-API-Key: …`

| 种类 | 环境变量 | 解析结果 | 典型用途 |
| --- | --- | --- | --- |
| 租户 Key | `AI_FLEET_API_KEY` 或 `AI_FLEET_TENANT_API_KEYS` JSON | `{tenant_id, agent_id: null}` | 注册/审批/挂起、建任务、改状态、列审计 |
| Agent Key | `AI_FLEET_AGENT_API_KEYS` JSON | `{tenant_id, agent_id}` | 发消息、回执 |

Agent Key 仅在对应 Agent **已注册且 Active** 时有效。

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
