# 05 · MCP 门面

## 为什么要 MCP

MCP 是「Agent → 工具」的垂直插座。各家宿主（Cursor、Claude Code、Codex…）已经会连 MCP Server。  
Fleet Kit 不发明新宿主协议，只把 Hub 的 HTTP 收成六个工具，让「会 MCP 的程序」变成协作参与方。

## 进程模型

每个 MCP 配置启动 **一个** `mcp_server.py` 子进程，环境变量决定身份：

| 变量 | 含义 |
| --- | --- |
| `FLEET_HUB_URL` | Hub 根地址，如 `http://127.0.0.1:8765` |
| `FLEET_API_KEY` | 租户 Key（建任务、改状态） |
| `FLEET_AGENT_API_KEY` | Agent Key（发消息、回执） |
| `FLEET_AGENT_ID` | 默认 `sender_agent_id` |

编排方与执行方应使用 **两份** MCP 配置（两套 Key），见 `fleet-kit/mcp-host.example.json`。

## 工具 ↔ HTTP

| MCP 工具 | Hub |
| --- | --- |
| `create_task(agent_id, title)` | `POST /tasks`（偏租户 Key） |
| `post_message(task_id, event_type, payload_json, …)` | `POST /tasks/{id}/messages` |
| `get_task(task_id)` | `GET /tasks/{id}` |
| `list_messages(task_id)` | `GET /tasks/{id}/messages` |
| `receipt(task_id, message_id)` | `POST .../receipt`（必须 Agent Key） |
| `set_status(task_id, status)` | `POST /tasks/{id}/status` |

返回值是 JSON 字符串：`{ok, status, data|error}`，便于宿主展示。

## 门面不做的事

- 不在本地再存一份任务状态（无状态）  
- 不解析「消息里写我是 admin」  
- 不实现 A2A JSON-RPC  
- 不推送；宿主必须再调 `list_messages` / `get_task` 才能看见更新
