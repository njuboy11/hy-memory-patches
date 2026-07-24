# simple_hybrid.md — SimpleHybrid Reader 完整改造说明
# ============================================================
# 适用版本: hy-memory (any compatible version)
# 升级后: 按以下自然语言描述重新打补丁（不要直接用 diff apply）

## 背景

为了在现有的向量语义召回（vec）基础上增加关键词精确匹配召回（BM25），
新建一个独立的 reader: `SimpleHybridReadPipeline`。

设计目标:
  - 三路召回 (Profile / Normal / Proactive) × 双通道 (vec + BM25)
  - 每路独立 vec∪BM25 合并去重 → Reranker → 截断
  - 不做 score fusion (无 0.6/0.4, 无 RRF)
  - 完全不动 Legacy / Hybrid V2 / Hybrid Tag

## 前提依赖

1. 必须已部署 `hy_memory/pipelines/_retrieval/reranker.py`（本仓库 server/reranker.py）
2. 必须安装 jieba + spaCy:
   ```bash
   cd /root/.hy-memory/.venv
   source bin/activate
   pip install jieba spacy
   python -m spacy download en_core_web_sm
   ```
3. 必须创建 jieba 用户词典（本仓库 server/jieba_userdict.txt）
4. 必须运行 BM25 重建脚本（本仓库 server/_reindex_bm25.py）
5. 启动脚本必须设置以下 env vars（见 system/start-server.sh）

---

## 新增文件列表

| 文件 | 用途 |
|------|------|
| `server/reader_simple_hybrid.py` | 新 reader 完整源码 (SimpleHybridReadPipeline) |
| `server/_reindex_bm25.py` | BM25 全量重建迁移脚本 |
| `server/jieba_userdict.txt` | jieba 中文用户词典 (86 个 HY 专有名词) |
| `system/start-server.sh` | 更新版启动脚本 (含新增 env vars) |

---

## 改动 1：注册新 reader (config.py)

**目的**: 在 reader dispatch 配置中注册 `simple_hybrid` 常量。

**查找方法**: 在 `hy_memory/pipelines/_retrieval/config.py` 搜索 `READER_MEM0`

**改动**: 在 `READER_MEM0 = "mem0"` 之后加一行:

    READER_SIMPLE_HYBRID = "simple_hybrid"

同时将 `READER_SIMPLE_HYBRID` 写入 `ALL_READERS` 元组。

---

## 改动 2：注册 dispatcher (reader.py)

**目的**: 在 reader dispatch 函数中添加 `simple_hybrid` 分支。

**查找方法**: 在 `hy_memory/pipelines/reader.py` 搜索 `# 默认 / fallback`

**改动**: 在 `# 默认 / fallback` 之前插入:

```python
    elif name == _retrieval_config.READER_SIMPLE_HYBRID:
        try:
            from .reader_simple_hybrid import SimpleHybridReadPipeline
            return SimpleHybridReadPipeline(config, embed_service, vector_store, graph_store=graph_store, cache=cache)
        except ImportError as e:
            logger.warning(f"[reader-dispatch] simple_hybrid import failed: {e}; fallback to legacy")
```

---

## 改动 3：start-server.sh 新增 env vars

**目的**: 添加 simple_hybrid 所需的环境变量。

**查找方法**: 在 `start-server.sh` 搜索 `MEMORY_ENABLE_AGENT`

**改动**: 在 `MEMORY_ENABLE_AGENT` 导出之后增加:

```bash
print(f'export HY_MEMORY_READER="simple_hybrid"')
print(f'export MEMORY_JIEBA_USERDICT="/root/.hy-memory/jieba_userdict.txt"')
print(f'export MEMORY_READER_VEC_MULT="3"')
print(f'export MEMORY_READER_BM25_MULT="1.5"')
```

**同时**: 将 `MEMORY_RERANKER_TIMEOUT` 从 "3000" 改为 "10000"（解决 3s 超时导致的 reranker fallback）。

---

## 改动 4：Reranker 超时修复

**原因**: 长 query (500+ 字符) + 30 条候选文档 → reranker body 6000+ token，
  3 秒超时 (`MEMORY_RERANKER_TIMEOUT=3000`) 导致 `httpx.ReadTimeout`，
  reranker 全部 fallback。

**查找方法**: 在 `start-server.sh` 搜索 `MEMORY_RERANKER_TIMEOUT`

**改动**:

    # 旧
    print(f'export MEMORY_RERANKER_TIMEOUT="3000"')

    # 新
    print(f'export MEMORY_RERANKER_TIMEOUT="10000"')

---

## 改动 5：Reranker 失败时 fallback 到 vec-only

**原因**: 原设计在 reranker 失败时仍然使用 vec+BM25 合并后的候选池排序。
  新设计: reranker 失败 → 丢弃 BM25 结果 → 纯 vec 候选排序 → 截断。

**查找方法**: 在 `reader_simple_hybrid.py` 的 `_merge_rerank_evolve` 方法中，
  搜索 `reranker_ok = any`

**改动** (已在 server/reader_simple_hybrid.py 中):

```python
# On reranker failure, fallback to vec-only (BM25 excluded per design)
if not reranker_ok:
    logger.info(
        f"[simple_hybrid] reranker {'failed' if len(merged) >= 2 else 'skipped'}; "
        f"bm25_recalled={bm25_total} new={bm25_new} duped={bm25_duped} "
        f"fallback to vec-only (top_k={top_k})"
    )
    vec_hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
    return vec_hits[:top_k]
```

---

## 改动 6：Source 标记 + BM25 去重日志

**原因**: 原 reader 不对召回结果标记 source 字段，无法区分结果是来自
  vec 还是 BM25。新增 source 标记和 BM25 召回计数日志。

**查找方法**: 在 `reader_simple_hybrid.py` 的 `_merge_rerank_evolve` 方法中，
  搜索 `source = "vec"` 和 `source = "bm25"`

**改动** (已在 server/reader_simple_hybrid.py 中):

- vec 命中标记 `hit["source"] = "vec"`
- BM25 命中标记 `hit["source"] = "bm25"`
- 记录 BM25 原始召回数、新增数、去重丢弃数、最终入 Reranker 数
- 在 `read()` 的最终日志中输出 source 分布 (如 `sources={'vec':5,'bm25':3}`)

---

## 调用方式

| 场景 | env var | reader |
|------|---------|--------|
| **simple_hybrid** | `HY_MEMORY_READER=simple_hybrid` | SimpleHybridReadPipeline |
| Legacy (默认) | `HY_MEMORY_READER=legacy` 或不设 | LegacyReadPipeline |
| Hybrid V2 | `HY_MEMORY_READER=hybrid_v2` | HybridV2ReadPipeline |

---

## 不打后果

- 不注册 simple_hybrid: 无法切换 reader，Legacy 继续工作但无 BM25 召回
- 不装 jieba/spaCy: BM25 分词回退到 raw text（空格切分），中文召回率大跌
- 不跑 _reindex_bm25.py: 旧 memory 的 BM25 向量用的是 Qdrant 原始 tokenization，
  与 jieba 用户词典不匹配，召回率降低
- 不提高 reranker timeout: 长 query 稳定触发 timeout → 全部 fallback 到 vec-only
