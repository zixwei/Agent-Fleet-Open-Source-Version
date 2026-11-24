# 05 · MCP 门面

## 为什么要 MCP

MCP 是「Agent → 工具」的垂直插座。各家宿主（Cursor、Claude Code、Codex…）已经会连 MCP Server。  
Fleet Kit 不发明新宿主协议，只把 Hub 的 HTTP 收成六个工具，让「会 MCP 的程序」变成协作参与方。

## 进程模型

每个 MCP 配置启动 **一个** `mcp_server.py` 子进程，环境变量决定身份：

| 变量 | 含义 |
| --- | --- |
| `FLEET_HUB_URL` | Hub 根地址，如 `http://127.0.0.1:8765` |
| `FLEET_AGENT_API_KEY` | 本宿主的 Agent Key：所有工具都用它 |
| `FLEET_AGENT_ID` | 默认 `sender_agent_id` |

编排方与执行方应使用 **两份** MCP 配置（两套 Agent Key），见 `fleet-kit/mcp-host.example.json`。**不要**把租户管理员 Key（`FLEET_API_KEY`）放进宿主配置；注册/审批/挂起 Agent 故意不做成工具。

## 工具 ↔ HTTP

| MCP 工具 | Hub |
| --- | --- |
| `create_task(agent_id, title)` | `POST /tasks`（调用方记为创建者，之后可取消） |
| `post_message(task_id, event_type, payload_json, …, idempotency_key, correlation_id, trace_id)` | `POST /tasks/{id}/messages`；超时重试时复用同一个 `idempotency_key`，不会产生第二条消息；回复时 `correlation_id` 填被回复的 `message_id`；`trace_id` 留空即沿用任务的 |
| `get_task(task_id)` | `GET /tasks/{id}` |
| `list_messages(task_id)` | `GET /tasks/{id}/messages` |
| `receipt(task_id, message_id)` | `POST .../receipt`（必须 Agent Key） |
| `set_status(task_id, status)` | `POST /tasks/{id}/status`（执行方推进；创建方只能取消；`409` = 被别人抢先改了） |

返回值是 JSON 字符串：`{ok, status, data|error}`，便于宿主展示。

## 门面不做的事

- 不在本地再存一份任务状态（无状态）  
- 不解析「消息里写我是 admin」  
- 不实现 A2A JSON-RPC  
- 不推送；宿主必须再调 `list_messages` / `get_task` 才能看见更新
