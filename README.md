# Agent-Fleet Open Source Version

Agent Fleet cross-vendor agent collaboration platform — open-source, self-hostable. A2A + MCP.

## The Problem

![A man overwhelmed because the AI agents around him do not share context](docs/problem-illustration.webp)

Different AI agents work in isolation: they do not know what the others are doing, they duplicate work, and they have no shared context — leaving one person to carry the confusion. Agent Fleet connects them.

## The Solution

![Agent Fleet in action: every agent in sync, sharing context on the same task](docs/solution-illustration.webp)

Agent Fleet in action: every agent in sync, sharing context on the same task.

## Contents

- [WHITEPAPER.md](WHITEPAPER.md) — 白皮书:为什么要修这条路,六层架构,18 项交付物.
- [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) — 环境冻结:PostgreSQL 18.7, Valkey 9.1.2.
- [prototype/](prototype/) — 阶段 0 原型:FastAPI + SQLite + 进程内消息总线,见 [prototype/README.md](prototype/README.md).
- [fleet-kit/](fleet-kit/) — **Fleet Kit 1.0** 可 fork 协作包（Hub + MCP 门面 + `./demo.sh`），见 [fleet-kit/README.md](fleet-kit/README.md).
