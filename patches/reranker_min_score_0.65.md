# MEMORY_RERANKER_MIN_SCORE: 0.6 → 0.65

## 目的

将 Simple Hybrid Reader 的 reranker 最低分阈值从 0.6 提高到 0.65，
进一步收紧记忆召回门槛，减少噪声记忆注入。

## 背景

当前阈值 0.6 在实际运行中表现出合理平衡（观湖国际 0.981、上风私募基金 0.930
等高相关性记忆通过；无关记忆如"我家住在哪"被拦截），但用户希望进一步降低
噪声，将阈值收紧到 0.65。

## 目标文件

`/root/.hy-memory/start-server.sh` — 第 56 行

## 适用版本

hy-memory v1.2.21+（所有使用 Simple Hybrid Reader 的部署）

## 改动点

### 1. start-server.sh 环境变量修改

第 56 行：

```bash
# 旧
print(f'export MEMORY_RERANKER_MIN_SCORE="0.6"')
# 新
print(f'export MEMORY_RERANKER_MIN_SCORE="0.65"')
```

### 2. 阈值生效路径（代码层面，无需手动改动）

该 env 变量被以下 2 个代码位置读取，改动后自动生效：

| 文件 | 行号 | 作用 | 描述 |
|------|------|------|------|
| `pipelines/_retrieval/reranker.py` | 45 | 读取 env | `os.getenv("MEMORY_RERANKER_MIN_SCORE", "0.1")` |
| `pipelines/_retrieval/reranker.py` | 113 | 主过滤 | `score >= self._config.min_score` |
| `pipelines/reader_simple_hybrid.py` | 536-537 | fallback 过滤 | reranker 挂了才走，`score >= min_score` |

上述 3 个位置全部通过同一个 env 变量 `MEMORY_RERANKER_MIN_SCORE` 获取阈值，
**只改 start-server.sh 一行即可全覆盖**。

### 3. 不受影响的参数

Simple Hybrid Reader 中以下 `score_threshold=0.3` 是 Qdrant/Kuzu **向量搜索召回前过滤**，
**不是** reranker 阈值，本次改动不影响它们：

| 文件行号 | 值 | 用途 |
|----------|-----|------|
| `reader_simple_hybrid.py:331` | `0.3` | Profile path Qdrant VEC recall |
| `reader_simple_hybrid.py:342` | `0.3` | Profile path Kuzu VEC recall |
| `reader_simple_hybrid.py:393` | `0.3` | Normal path Qdrant VEC recall |

## 三条 Path 统一受影响

Simple Hybrid 的 3 条召回路径都汇聚到同一个 `_merge_rerank_evolve` 方法，
调用唯一一次 `self._reranker.rerank()`，阈值统一由 env 控制：

- **Profile path** (`_search_profile`) → `_merge_rerank_evolve` → `reranker.rerank()` → `min_score=0.65`
- **Normal path** (`_search_normal`) → `_merge_rerank_evolve` → `reranker.rerank()` → `min_score=0.65`
- **Proactivity path** (`_search_proactive`) → `_merge_rerank_evolve` → `reranker.rerank()` → `min_score=0.65`

## 设计要点

- 阈值从 env 变量注入而非代码硬编码，方便动态调整
- 3 条 path 共享同一阈值，一致性有保障
- 修改后重启服务即可生效，无需改 Python 源码

## 验证

```bash
# 1. 查看当前配置
grep MEMORY_RERANKER_MIN_SCORE /root/.hy-memory/start-server.sh
# 应输出: print(f'export MEMORY_RERANKER_MIN_SCORE="0.65"')

# 2. 重启服务
systemctl --user restart hy-memory-server

# 3. 验证进程环境变量
cat /proc/$(pgrep -f hy_memory.server | head -1)/environ | tr '\0' '\n' | grep MEMORY_RERANKER_MIN_SCORE
# 应输出: MEMORY_RERANKER_MIN_SCORE=0.65
```

## 恢复流程

```bash
# 覆盖启动脚本
cp system/start-server.sh /root/.hy-memory/start-server.sh

# 重启服务
systemctl --user restart hy-memory-server

# 验证
cat /proc/$(pgrep -f hy_memory.server | head -1)/environ | tr '\0' '\n' | grep MEMORY_RERANKER_MIN_SCORE
```
