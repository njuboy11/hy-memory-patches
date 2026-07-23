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

## 详细修改

### 1. start-server.sh
- **LLM endpoint**: `https://api.minimaxi.com/anthropic` → `https://api.minimaxi.com/v1`
- **LLM provider**: `anthropic` → `openai`
- 原因：LLMProvider 内部对所有 provider 走 OpenAI backend，Anthropic endpoint 没有 `/chat/completions` 路由

- 新增 `MEMORY_ENABLE_SUMMARY=true`：开启 L3 滚动摘要
- 新增 `HY_MEMORY_THINKING_MODE=adaptive`：MiniMax M3 开 thinking 改善 JSON 输出

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

## 升级恢复流程

```bash
# 1. 升级 hy-memory pip 包后
# 2. 覆盖启动脚本
cp server/start-server.sh /root/.hy-memory/start-server.sh

# 3. 应用 server.py patch
patch /root/.hy-memory/.venv/lib/python3.12/site-packages/hy_memory/server.py < server/server.py.patch

# 4. 重启服务
kill -9 $(pgrep -f hy_memory.server) && systemctl --user start hy-memory-server

# 5. 恢复 crontab
crontab -l | grep -v digest > /tmp/cron.tmp
echo "0 3 * * * curl ..." >> /tmp/cron.tmp
crontab /tmp/cron.tmp
```
