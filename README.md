# hy-memory 修改记录 (OpenClaw 部署)

hy-memory v1.2.21 在 OpenClaw VM100 上部署所需的修改，升级后需要重新应用。

## 修改概览

| 文件 | 位置 | 用途 |
|------|------|------|
| `start-server.sh` | `/root/.hy-memory/start-server.sh` | 启动脚本（完整覆盖） |
| `server.py` | hy_memory/server.py (site-packages) | 加 digest HTTP 端点 + M3 thinking |
| `hy-memory-server.service` | `/root/.config/systemd/user/` | systemd 服务单元 |
| `openclaw.json` | plugins.entries.openclaw-hy-memory | 插件 hook 超时配置 |
| `crontab` | `crontab -l` | 每天凌晨自动 digest |
| `reranker.py` | hy_memory/pipelines/_retrieval/reranker.py | SiliconFlow Qwen3-Reranker-8B |
| `reader_legacy.py` | hy_memory/pipelines/reader_legacy.py | OVERFETCH 3x + reranker 注入 |

## 详细修改

### 1. start-server.sh
- **LLM endpoint**: `https://api.minimaxi.com/anthropic` → `https://api.minimaxi.com/v1`
- **LLM provider**: `anthropic` → `openai`
- 原因：LLMProvider 内部对所有 provider 走 OpenAI backend，Anthropic endpoint 没有 `/chat/completions` 路由

- 新增 `MEMORY_ENABLE_SUMMARY=true`：开启 L3 滚动摘要
- 新增 `HY_MEMORY_THINKING_MODE=disabled`：关 M3 thinking 避免 JSON parse 污染

### 2. server.py 修改
**位置**: `/root/.hy-memory/.venv/lib/python3.12/site-packages/hy_memory/server.py`

变更 A - 加 /api/v1/digest 端点（使 System 2 可通过 HTTP 触发，避免停服）:
- `do_POST` 方法加路由: `if path == "/api/v1/digest": self._handle_digest(body)`
- 新增 `_handle_digest` 方法

变更 B - M3 thinking 白名单:
- 在 `_needs_thinking_body` 条件中加 `or "minimax" in _llm_model`

### 3. openclaw.json 插件配置
```json
"hooks": {
  "allowConversationAccess": true,
  "timeoutMs": 90000
}
```
- agent_end hook 超时从默认 30s → 90s（add 全链路 L1+L2+L3 需要 60-80s）

### 4. 系统 crontab
```
0 3 * * * curl -sS -X POST http://127.0.0.1:19527/api/v1/digest -H 'Content-Type: application/json' -d '{"user_id":"wangdapeng","agent_id":"main"}' --max-time 300 >> /var/log/hy-memory-digest.log 2>&1
```
- 每天凌晨 3 点自动触发 System 2 digest

### 5. Reranker — SiliconFlow Qwen3-Reranker-8B (2026-07-24)

在 legacy reader 召回链路中注入 reranker，提升记忆召回质量。

**涉及文件**:
- **新建**: `server/reranker.py` → `hy_memory/pipelines/_retrieval/reranker.py`
- **修改**: `server/reader_legacy.patch` → `hy_memory/pipelines/reader_legacy.py`
- **修改**: `system/start-server.sh` — 新增 7 个环境变量

**改动摘要**:

| 参数 | 旧值 | 新值 | 说明 |
|------|------|------|------|
| OVERFETCH | 1.5 | **3** | 候选池倍数 |
| 新增 MEMORY_RERANKER_ENABLED | - | **true** | 开启 reranker |
| 新增 MEMORY_RERANKER_URL | - | **https://api.siliconflow.cn/v1/rerank** | API 端点 |
| 新增 MEMORY_RERANKER_MODEL | - | **Qwen/Qwen3-Reranker-8B** | 模型 |
| 新增 MEMORY_RERANKER_TIMEOUT | - | **3000** | 超时(ms) |
| 新增 MEMORY_RERANKER_MAX_CANDIDATES | - | **50** | 最大候选数 |
| 新增 MEMORY_RERANKER_MIN_SCORE | - | **0.6** | 最低分阈值 |

**reranker 注入点**: `expand_evolution_chains` 之后, sort+truncate 之前。
三路 (Profile/Normal/Proactive) 各自独立调用。
阈值 0.6 过滤：全低于阈值返回 []（不注入），API 异常才 fallback 原向量分。

## 升级恢复流程

```bash
# 1. 升级 hy-memory pip 包后
# 2. 覆盖启动脚本
cp system/start-server.sh /root/.hy-memory/start-server.sh

# 3. 应用 server.py patch
patch /root/.hy-memory/.venv/lib/python3.12/site-packages/hy_memory/server.py < server/server.py.patch

# 4. 复制 reranker 模块（新文件）
cp server/reranker.py /root/.hy-memory/.venv/lib/python3.12/site-packages/hy_memory/pipelines/_retrieval/reranker.py

# 5. 手动 apply reader_legacy.py 的 4 处改动（见 server/reader_legacy.patch）
#    - import reranker 模块
#    - OVERFETCH 1.5→3
#    - __init__ 新增 reranker 初始化
#    - sort+truncate 前注入 reranker 调用

# 6. 重启服务
kill -9 $(pgrep -f hy_memory.server) && systemctl --user start hy-memory-server

# 7. 恢复 crontab
crontab -l | grep -v digest > /tmp/cron.tmp
echo "0 3 * * * curl ..." >> /tmp/cron.tmp
crontab /tmp/cron.tmp
```


### 6. GET /api/v1/graph/list_schemas 端点 (2026-07-25)

**目的**：让 OpenClaw agent 通过 HTTP 查询 Kuzu 图数据库中的所有 L6 Schema 节点，
避免因为 Kuzu 单进程独占锁而需要停服。

**涉及文件**:
- **修改**: `server/server.py` → `hy_memory/server.py`
- **描述**: `server/list_schemas_endpoint.md`

**改动摘要**:
- `do_GET` 路由注册 `/api/v1/graph/list_schemas`
- 新增 `_handle_graph_list_schemas` handler 方法（同步走 graph_store._execute）

**使用方式**:
```
curl http://127.0.0.1:19527/api/v1/graph/list_schemas
```
返回 `{"schemas": [...], "count": 7}`

**恢复流程**:
```
# 应用 server.py patch（需先应用 digest.patch，然后叠加 list_schemas 改动）
# 参见 server/list_schemas_endpoint.md 的自然语言描述
# 重启服务
systemctl --user restart hy-memory-server.service
```


### 7. GET /api/v1/busy 端点 (2026-07-25)

**目的**：让 Kuzu 备份脚本在停服前精确判断 capture 是否正在进行，
避免在 LLM 提取中途杀进程导致 L2/L3 结果丢失。

**涉及文件**:
- **修改**: `server/server.py` → `hy_memory/server.py`
- **描述**: `server/busy_endpoint.md`

**改动摘要**:
- `import threading` + 全局 `_active_add_count` 计数器 + `_active_add_lock`
- `do_GET` 路由注册 `/api/v1/busy`
- `_handle_add` 入口 `+= 1`、出口 `finally -= 1`（线程安全）
- 新增 `_handle_busy` handler 返回 `{"busy": true/false, "active_add_requests": N}`


### 8. Reranker 阈值收紧: 0.6 → 0.65 (2026-07-26)

**目的**：将 Simple Hybrid Reader 三路 reranker 最低分阈值从 0.6 提高到 0.65，
进一步减少噪声记忆注入。

**涉及文件**:
- **修改**: `system/start-server.sh` — `MEMORY_RERANKER_MIN_SCORE` 从 0.6 改为 0.65
- **描述**: `patches/reranker_min_score_0.65.md`

**改动摘要**:
| 参数 | 旧值 | 新值 | 说明 |
|------|------|------|------|
| MEMORY_RERANKER_MIN_SCORE | 0.6 | 0.65 | reranker 最低分阈值 |

**生效范围**：3 条 path（Profile / Normal / Proactivity）统一受影响，
fallback 路径也同步生效。只改 env 变量一行，无需改 Python 源码。

**恢复流程**:
```bash
cp system/start-server.sh /root/.hy-memory/start-server.sh
systemctl --user restart hy-memory-server
cat /proc/$(pgrep -f hy_memory.server | head -1)/environ | tr '\0' '\n' | grep MEMORY_RERANKER_MIN_SCORE
```
