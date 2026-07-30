# MEMORY_AGENT_MAX_TOKENS: 2000 → 10000

## 目的

将 System2 digest agent 的 LLM `max_tokens` 从默认 2000 提升到 10000，防止 LLM 输出 JSON 被截断导致 tool_calls 为空、fresh facts 全部未消化。

## 背景

Digest endpoint (`POST /api/v1/digest`) 调用 System2 agent 对 96+ 条 L2 fresh facts 做聚类和 schema 提取。LLM 输出为 JSON 格式的 tool_calls 数组，内容包含 `evidence_list`（UUID 列表）和 `content`（schema 描述文本）。

实际运行中发现：当 fresh facts 数量较多时（3 clusters × 84 + 12 unclustered ≈ 96 条），LLM 输出的 JSON 在 `evidence_list` 数组中间被截断（`completion_tokens=2000` 撞到上限），截断位置在逗号 `...,"uuid",` 未闭合，导致 endpoint JSON 解析失败 → tool_calls 为空 → 所有 fresh facts 丢失（`s2_evidence_count` 未递增）。

**症状**：
- digest HTTP 200，但 `system2_agent.tool_calls` 为空数组
- 日志中可见 `completion_tokens=2000`（恰好等于默认上限）
- `agent_reasoning` 的 JSON 在 evidence_list 数组中不完整

**影响**：
- 2026-07-31 digest 连续多次因截断消化失败
- 96 条 fresh facts 未能转为 L6 schema 和 evidence

## 目标文件

`/root/.hy-memory/.venv/lib/python3.12/site-packages/hy_memory/config.py` — 第 196 行

## 适用版本

hy-memory v1.2.21+（所有使用 System2 agent 的部署）

## 改动点

### 方案 A：systemd 环境变量（生产推荐，零代码改动）

在 `hy-memory-server.service` 的 `[Service]` 段添加：

```
Environment="MEMORY_AGENT_MAX_TOKENS=10000"
```

然后 `systemctl --user daemon-reload && systemctl --user restart hy-memory-server`。

此环境变量被以下调用链读取：

| 文件 | 行号 | 作用 |
|------|------|------|
| `hy_memory/config.py` | 196 | `_get_env_int("MEMORY_AGENT_MAX_TOKENS", 2000)` → 默认值 |
| `hy_memory/pipelines/system2_agent.py` | 612 | `max_tokens=config.llm.agent_max_tokens or 4000` → 传给 LLM |

### 方案 B：代码默认值（通用兜底）

config.py 第 196 行：

```python
# 旧
self.agent_max_tokens = _get_env_int("MEMORY_AGENT_MAX_TOKENS", 2000)
# 新
self.agent_max_tokens = _get_env_int("MEMORY_AGENT_MAX_TOKENS", 10000)
```

### 方案 B patch（针对站点包文件）

```diff
--- a/config.py
+++ b/config.py
@@ -196,7 +196,7 @@
         if self.agent_max_tokens is None:
-            self.agent_max_tokens = _get_env_int("MEMORY_AGENT_MAX_TOKENS", 2000)
+            self.agent_max_tokens = _get_env_int("MEMORY_AGENT_MAX_TOKENS", 10000)
```

## 验证

```bash
# 检查运行中进程的环境变量
cat /proc/$(pgrep -f hy_memory.server)/environ | tr '\0' '\n' | grep MEMORY_AGENT_MAX_TOKENS

# 预期输出
MEMORY_AGENT_MAX_TOKENS=10000
```

## 相关问题

- 2000 tokens 上限对 evidence_list 较重（30+ UUID）的 digest 不够用
- 提高到 10000 后实测通过：2026-07-31 07:00 补跑成功消化 96 条 fresh facts，产出 4 条新 L6 schema + 34 条 evidence
- system2_agent.py:612 的 fallback `or 4000` 在 env var 设置后不再触发
