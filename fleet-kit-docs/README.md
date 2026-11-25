# Fleet Kit 1.0 工作原理（详解）

本目录只说明 **Fleet Kit 1.0** 怎么工作，不改动白皮书、`docs/`、`prototype/`。

对应代码：仓库根目录 [`../fleet-kit/`](../fleet-kit/)。

| 文档 | 内容 |
| --- | --- |
| [01-overview.md](01-overview.md) | 要解决什么、不解决什么、和白皮书关系 |
| [02-architecture.md](02-architecture.md) | 组件图、进程边界、数据落在哪 |
| [03-message-flow.md](03-message-flow.md) | 一次协作从注册到完成的逐步时序 |
| [04-identity-tenancy.md](04-identity-tenancy.md) | 密钥种类、租户隔离、谁能挂起谁 |
| [05-mcp-facade.md](05-mcp-facade.md) | MCP 六个工具如何映射到 Hub HTTP |
| [06-realtime-and-limits.md](06-realtime-and-limits.md) | 「实时」含义、推送/A2A 边界 |
| [07-demo-and-acceptance.md](07-demo-and-acceptance.md) | `demo.sh`、验收清单、Cursor MCP 验收步骤 |

读完顺序建议：01 → 02 → 03 → 其余按需。
