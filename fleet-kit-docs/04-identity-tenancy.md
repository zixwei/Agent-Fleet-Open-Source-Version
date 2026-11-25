# 04 · 身份与租户

## 两种凭证

全部走 HTTP 头：`X-API-Key: …`

| 种类 | 环境变量 | 解析结果 | 典型用途 |
| --- | --- | --- | --- |
| 租户 Key | `AI_FLEET_API_KEY` 或 `AI_FLEET_TENANT_API_KEYS` JSON | `{tenant_id, agent_id: null}` | 注册/审批/挂起、运维兜底改状态、列审计 |
| Agent Key | `AI_FLEET_AGENT_API_KEYS` JSON | `{tenant_id, agent_id}` | 建任务、发消息、回执、推进自己被指派的任务 |

| 运维 Key（可选） | `AI_FLEET_OPERATOR_API_KEY` | `{tenant_id: "sys:operator"}` | 只能调 `/ops/*`：跨租户审计视图、完整链校验 |

运维 Key 至少 24 个字符（由 `scripts/gen_keys.py` 签发时保证），且不能和任何租户 Key、Agent Key 相同（为此四个 Key 变量的全部条目须是同一种哈希：全 `hmac-sha256` 且设了 pepper，或全 `sha256`；混用同样启动报错），否则 `/ops/*` 全部关闭、启动检查报错；它过不了租户或 Agent 接口，租户和 Agent Key 也过不了 `/ops/*`。比较用常数时间。

上表四个变量里放的都是 **Key 的哈希**，不是 Key 本身，见下一节。

**租户 id 格式**：`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`，不能含 `:`。`sys:` 前缀留给系统（`sys:unauthenticated`、`sys:operator`）。格式不对的 Key 一律 `401`，启动时 `validate_key_config()` 报错拒绝启动（报错只写租户 id，不写 Key）。

Agent Key 仅在对应 Agent **在该 Key 所属租户内已注册且 Active** 时有效；Key 里写的 Agent 不在本租户 → `401`。

**租户 Key 不进任何 MCP 宿主配置。** 宿主只放自己的 Agent Key（见 `mcp-host.example.json`）；`mcp_server.py` 发现 `FLEET_API_KEY` 会忽略并提示。任务状态的权限见 [03 阶段 5](03-message-flow.md)。

## 只存哈希（B2）

Hub 不再持有、也不再比较明文 Key。`AI_FLEET_TENANT_API_KEYS` / `AI_FLEET_AGENT_API_KEYS` 的 JSON 键、`AI_FLEET_API_KEY`、`AI_FLEET_OPERATOR_API_KEY` 的值都必须是以下两种形式之一：

| 形式 | 含义 |
| --- | --- |
| `sha256:<64 位十六进制>` | Key（UTF-8）的 SHA-256 |
| `hmac-sha256:<64 位十六进制>` | 以 `AI_FLEET_KEY_PEPPER`（服务端 pepper，≥ 32 字符）为密钥的 HMAC-SHA256 |

客户端照旧在 `X-API-Key` 里出示明文 Key；Hub 把它按上述方案算哈希，与**每一条**配置项用 `hmac.compare_digest` 做常数时间比较（不在第一条命中处提前返回）。Key 由 `secrets` 生成 32 字节随机数（256 bit），所以 SHA-256 足够，不需要 bcrypt 之类的慢哈希；pepper 是可选的第二道秘密：只泄露 Key 配置时，连离线验证猜测都做不到。

**启动即拒绝（fail closed）**：只要任一变量里还有明文形式的 Key，`validate_key_config()` 报错、Hub 拒绝启动。报错写变量名和条目数（如 `AI_FLEET_TENANT_API_KEYS: 1 of 3 entries are not in hashed form; ...`），**绝不写 Key 的值**。即使绕过了启动检查，请求时明文条目也永远匹配不上。用了 `hmac-sha256:` 却没设 pepper、pepper 太短、JSON 不合法，同样拒绝启动。演示默认值 `AI_FLEET_API_KEY` 已删除：不设就是不接受单一 Key。

### 迁移（现有部署必须重新签发 Key）

旧 Key 曾以明文存在配置、日志或 CI 变量里，视为已暴露；不要把旧 Key 直接算哈希接着用，而是**逐个重新签发**：

1. 每个租户 / Agent / 运维 Key 各跑一次（可选：先 `export AI_FLEET_KEY_PEPPER=...`，Hub 也要设同一个值）：
   ```bash
   cd fleet-kit
   python3 scripts/gen_keys.py new
   # key:  <只给客户端，只显示这一次>
   # hash: sha256:<写进 Hub 配置>
   ```
2. 把 `key` 交给对应客户端（MCP 宿主的 `FLEET_AGENT_API_KEY`、调用方的 `X-API-Key`）。
3. 把 `hash` 写进 Hub：`AI_FLEET_TENANT_API_KEYS='{"sha256:…": "tenant-a"}'`、`AI_FLEET_AGENT_API_KEYS='{"sha256:…": {"agent_id": "worker", "tenant_id": "tenant-a"}}'`、`AI_FLEET_OPERATOR_API_KEY=sha256:…`、`AI_FLEET_API_KEY=sha256:…`。
4. 重启 Hub。若启动报 `... is not in hashed form`，按报错里的变量名找到残留的明文项。
5. 删除所有旧明文 Key（环境变量、`.env`、CI secrets、shell 历史）。

外部密钥系统签发的 Key（≥ 24 字符）可以只算哈希：`python3 scripts/gen_keys.py hash < key.txt`（从 stdin 读，不进 argv / shell 历史）。不用脚本时：

```bash
key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')
printf 'sha256:%s\n' "$(printf %s "$key" | openssl dgst -sha256 -r | cut -d' ' -f1)"
```

## 暴力破解退避（B2）

`app/identity/attempts.py`。只统计「出示了 Key 且校验失败」（401）；没带 Key 不算猜测，不计数。退避期间直接 **429 + `Retry-After`**，Key 根本不校验（不给攻击者任何答案，也不耗 Hub 的算力）。

| 作用域 | 键 | 触发 | 解除 / 重置 |
| --- | --- | --- | --- |
| 凭证 | 出示值的指纹（进程内随机密钥的 HMAC，不是 Key，也不是配置里的哈希） | 同一个值连续失败 `AI_FLEET_AUTH_FAIL_PER_CREDENTIAL` 次 | 成功一次即清零；封禁过后再失败，每次等待翻倍，上限 `AI_FLEET_AUTH_BACKOFF_MAX_SECONDS` |
| 来源 | 直连对端地址（不信任 `X-Forwarded-For`） | 窗口内失败 `AI_FLEET_AUTH_FAIL_PER_SOURCE` 次 | 只对**未证明**的 Key 生效；成功**不**重置（否则代理后的正常流量会替攻击者洗掉失败计数） |

**为什么锁不住共享代理后的正常客户端**：凭证作用域只在「这个值校验失败」时累加，正常 Key 校验成功，谁也没法把它推进退避；来源作用域对最近成功过的 Key（`AI_FLEET_AUTH_KNOWN_GOOD_TTL_SECONDS` 内）放行，而「最近成功过」没有 Key 就伪造不了。代价只落在还没证明过自己的 Key 上（所有猜测都在这一类），而且有上限、自动解除，不需要人工解锁。不做服务端 sleep：挂起的请求占着工作线程，攻击者可借此拖垮正常请求。

`request.denied` 审计里多了 429，同样经过 DenialLimiter，所以刷 429 也撑不大审计表；DenialLimiter 自身仍然只决定怎么记、从不拒绝请求。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AI_FLEET_AUTH_FAIL_WINDOW_SECONDS` | 300 | 计数窗口；一个窗口内没有新失败且不在封禁中 → 遗忘 |
| `AI_FLEET_AUTH_FAIL_PER_CREDENTIAL` | 5 | 同一值连续失败几次开始退避 |
| `AI_FLEET_AUTH_FAIL_PER_SOURCE` | 500 | 同一来源窗口内失败几次开始退避；默认很宽，因为代理后这是所有客户端共用的额度；不经代理直连的部署可调低 |
| `AI_FLEET_AUTH_BACKOFF_BASE_SECONDS` | 1 | 第一次封禁时长 |
| `AI_FLEET_AUTH_BACKOFF_MAX_SECONDS` | 300 | 封禁时长上限 |
| `AI_FLEET_AUTH_KNOWN_GOOD_TTL_SECONDS` | 86400 | 成功过的 Key 免受来源作用域限制的时长 |
| `AI_FLEET_AUTH_MAX_TRACKED` | 10000 | 每张表最多记录条数（按最近使用淘汰），内存有上界 |
| `AI_FLEET_KEY_PEPPER` | 未设 | 可选；设了就用 `hmac-sha256:` 形式 |

已知边界：状态在进程内存里，N 个 worker 进程 = N 倍额度，重启清零。真正挡住猜测的是 256 bit 随机 Key；退避让在线猜测变慢、变得可见。

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
