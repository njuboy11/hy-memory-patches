# Reranker 阈值 0.65 → 0.7

**日期**：2026-08-23
**改动**：把 MEMORY_RERANKER_MIN_SCORE 从 0.65 提升到 0.7

## 背景

2026-07-27 提交 `070dfa6` 把阈值从 0.6 调到 0.65，原因是 Reranker 召回命中数偏多、需要更严格的过滤。
到 2026-08-23，鹏哥观察发现 0.65 下仍然有噪声记忆注入，决定进一步收紧到 0.7。

## 改点

只动一处：`/root/.hy-memory/start-server.sh:42`

```diff
- print(f'export MEMORY_RERANKER_MIN_SCORE="0.65"')
+ print(f'export MEMORY_RERANKER_MIN_SCORE="0.7"')
```

## 全链路验证（再次确认）

通过 `/proc/<PID>/environ` 直接读进程环境变量，验证所有读取链路：

```
HY_MEMORY_READER=simple_hybrid
MEMORY_RERANKER_ENABLED=true
MEMORY_RERANKER_MIN_SCORE=0.7
```

### 链路 1：`RerankerConfig.__init__`（reranker.py:44-46）

```python
self.min_score: float = float(
    os.getenv("MEMORY_RERANKER_MIN_SCORE", "0.1")
)
```

读 env 的 0.7，覆盖 default 0.1。

### 链路 2：fallback 路径（reader_simple_hybrid.py:536）

```python
min_score = self._reranker_config.min_score if self._reranker else 0.3
filtered = [h for h in vec_hits if h.get("score", 0.0) >= min_score]
```

`self._reranker_config.min_score` 就是链路 1 的 0.7，fallback 路径用同一个值。

### 链路 3：3 条 path 共用

3 条 path（Profile / Normal / Proactive）共享同一个 `self._reranker` 管道，
最终走 `self._reranker.rerank()`，min_score 都是 0.7。

## 部署步骤

1. 编辑 `/root/.hy-memory/start-server.sh` 第 42 行
2. `kill -9 <旧PID>` 强制杀掉旧 hy-memory-server 进程
3. 重启服务（新进程自动通过 `start-server.sh` 拉起，带新 env）
4. 验证：`cat /proc/<新PID>/environ | tr '\0' '\n' | grep MEMORY_RERANKER_MIN_SCORE`

## 影响预估

- 0.65 → 0.7 是收紧，过滤门槛更高
- 预计注入记忆的命中率会下降（噪声更少、召回更精）
- 如果发现召回变空（极端情况），可回退到 0.65
- 配套的 Qdrant `score_threshold=0.3` 不受影响（那是向量召回前的软过滤，跟 Reranker 阈值独立）

## 与之前改动的对比

| 时间 | 改动 | commit |
|---|---|---|
| 2026-07-26 | 0.6 → 0.65 | `070dfa6` |
| 2026-08-23 | 0.65 → 0.7 | 本次 |
