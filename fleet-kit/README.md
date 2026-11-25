# Fleet Kit 1.0

可 fork 的多 Agent 协作模式：**Hub 任务账本 + MCP 工具门面 + 无密钥双进程演示**。

这不是网页、App 或插件；也不是 A2A 认证平台。实时 = 对方下一次调工具 / 短轮询可见。

## 一条命令

```bash
cd fleet-kit
chmod +x demo.sh
./demo.sh
```

无模型密钥。成功时退出码 0，日志含两个 agent id、同一个 `task_id`、终态 `completed`、receipt、审计通过、跨租户 404、MCP 工具打到同一 Hub。

## 可改配置

1. `FLEET_HUB_URL` / 端口（默认 `http://127.0.0.1:8765`）
2. `FLEET_API_KEY` 与两个 agent key：`demo.sh` 每次运行用 `scripts/gen_keys.py` 现场生成随机值（也可预设 ≥ 24 字符的值）。Hub 侧所有 Key 变量只放**哈希**（`sha256:…` / `hmac-sha256:…`），明文配置启动即拒绝；签发与迁移见 [04 身份与租户](../fleet-kit-docs/04-identity-tenancy.md#只存哈希b2)
3. （可选）运维 Key `AI_FLEET_OPERATOR_API_KEY`（`python3 scripts/gen_keys.py new` 签发，配置其哈希；不与任何租户/Agent Key 相同）：开启 `/ops/audit`、`/ops/audit/verify`
4. MCP 宿主：`python3 scripts/gen_keys.py demo --env-file .demo-keys.env --mcp-host mcp-host.json` 生成填好路径和随机 Agent Key 的 `mcp-host.json`（模板 [mcp-host.example.json](mcp-host.example.json) 只有占位符），贴进 Cursor / Claude Code 等 MCP 配置，并用 `.demo-keys.env` 启动 Hub。宿主里只放 **Agent Key**，租户管理员 Key 不进宿主配置
5. 认证失败退避：`AI_FLEET_AUTH_*`（同一错误 Key 连续 5 次后 429，详见 04）

## MCP 工具

`create_task` · `post_message` · `get_task` · `list_messages` · `receipt` · `set_status`

身份只来自 `FLEET_AGENT_API_KEY`，不信任消息正文自称。`post_message` 支持 `idempotency_key`，超时重试复用同一个值即可。执行方推进自己的任务，创建方只能取消。回复时用 `correlation_id` 指向被回复的消息；`GET /tasks/{id}/correlation` 核对一个任务的消息、回执、状态与审计是否全部对得上（T09）。

## 非目标（1.0 不做）

- A2A conformance / Agent Card 验签
- 推送到厂商进程内的长连接
- 沙箱、成本熔断、Postgres/Valkey 接线
- 前端 / 专用插件

白皮书仍是长期蓝图；本目录是当前可运行的复制包。原仓库 `prototype/`、`WHITEPAPER.md`、`docs/` 未改动。

## 测试

```bash
cd fleet-kit
python3 -m pip install --user -r requirements.txt
python3 -m pytest tests/ -q
```

## 协议字段

消息信封 `protocol` 为 `fleet`（不是 `a2a` 占位）。
