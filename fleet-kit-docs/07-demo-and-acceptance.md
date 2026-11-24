# 07 · 演示与验收

## 绿路径（无模型 Key）

```bash
cd fleet-kit
chmod +x demo.sh
./demo.sh
```

`demo.sh` 会：

1. 装依赖  
2. 配置演示用租户 Key / 两个 Agent Key / 另一租户 Key  
3. 在 `FLEET_PORT`（默认 8765）起 Hub；**自动把 `FLEET_HUB_URL` 对齐到该端口**  
4. 跑 `scripts/demo_two_agents.py`（注册→任务→消息→回执→完成→跨租户 404→审计）  
5. 跑 `scripts/mcp_http_parity.py`（MCP 工具函数打同一 Hub）  
6. 退出码 0 打印 `demo.sh PASS`

单测：

```bash
cd fleet-kit
python3 -m pytest tests/ -q
```

CI：`.github/workflows/fleet-kit.yml` 在改动 `fleet-kit/` 的 PR 上跑单测和 `demo.sh`。

## Cursor 当仓库外宿主（S3）

1. 先保证 Hub 在跑（可改 demo 不杀进程，或手动）：
   ```bash
   cd fleet-kit
   export AI_FLEET_API_KEY=fleet-demo-tenant
   # 按需导出 AI_FLEET_TENANT_API_KEYS / AI_FLEET_AGENT_API_KEYS（与 demo.sh 一致）
   python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8765
   ```
2. Cursor → Settings → MCP，粘贴并改路径：
   - 参考 [`../fleet-kit/mcp-host.example.json`](../fleet-kit/mcp-host.example.json)
   - `/ABS/PATH/TO/fleet-kit` → 本机绝对路径  
   - `FLEET_HUB_URL` → `http://127.0.0.1:8765`
3. 重载 MCP，确认出现 `create_task`、`post_message` 等工具  
4. 在对话里让 Cursor 调用一次 `create_task` 或 `get_task`  
5. 用返回的 `task_id` 对照 `curl -H "X-API-Key: …" http://127.0.0.1:8765/tasks/{id}`  
6. 通过即：仓库外宿主与 Kit 共用同一账本

编排方 / 执行方建议配 **两个** MCP server 条目（两套 Agent Key）。

## 验收对照表

| 项 | 证据 |
| --- | --- |
| 无密钥绿路径 | `./demo.sh` → 0 |
| 双 Agent + 单任务 | 日志 `orchestrator` / `worker` + 同一 `task_id` |
| 回执幂等 | 第二次 receipt `stored: false` |
| 客户端消息幂等 | HTTP 与 MCP 两条路径各重试一次同一 `idempotency_key`；消息列表长度不增 |
| 最小权限 | Agent Key 调管理接口 `403`；非执行方改状态 `403`；MCP 路径全程不带租户 Key |
| 跨租户 | 另一租户 Key → 404 |
| 审计 | `/audit/verify` 校验整条链 → `ok: true`（超过 1000 条且有篡改的情况见 `tests/test_audit_verify.py`） |
| 审计关联（T09） | `GET /tasks/{id}/correlation` → `ok: true`，3/3 条消息、2/2 个回执对得上审计；被拒请求有 `request.denied` 记录（见 `tests/test_t09_correlation.py`） |
| 审计访问范围 | 租户管理员 `/audit/verify` 只得到 `{"ok"}`；Agent Key `403`；明细只在 `/ops/*`（运维 Key）；未认证拒绝记在 `sys:unauthenticated`，没有租户读得到（见 `tests/test_audit_access.py`） |
| 审计防刷 | 300 次错误 Key 只产生 1 条单独记录 + 窗口结束后 1 条汇总；链校验仍通过（见 `tests/test_denial_flood.py`） |
| MCP 同账本 | `mcp_http_parity` 或 Cursor 工具留下同一 `task_id` |
| 口径 | README/`protocol` 为 `fleet`，无「已兼容 A2A」 |

## 目录索引

| 路径 | 作用 |
| --- | --- |
| `fleet-kit/app/` | Hub |
| `fleet-kit/mcp_server.py` | MCP 门面 |
| `fleet-kit/demo.sh` | 一键绿路径 |
| `fleet-kit/mcp-host.example.json` | 宿主粘贴模板 |
| `fleet-kit-docs/` | 本原理说明（本目录） |
