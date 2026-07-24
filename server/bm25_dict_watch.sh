#!/bin/bash
# 每天凌晨 3:30 检测自定义词典文件是否有变更（24h 内被修改过）
# 有变更则调用 reindex_bm25 接口全量重建 BM25 稀疏向量

set -e

JIEBA_DICT="/root/.hy-memory/jieba_userdict.txt"
SPACY_DICT="/root/.hy-memory/spacy_phrasedict.txt"
REINDEX_URL="http://127.0.0.1:19527/api/v1/reindex_bm25"
LOG="/var/log/bm25_dict_watch.log"

now=$(date +%s)
yesterday=$((now - 86400))

jieba_mtime=$(stat -c %Y "$JIEBA_DICT" 2>/dev/null || echo 0)
spacy_mtime=$(stat -c %Y "$SPACY_DICT" 2>/dev/null || echo 0)

changed_files=""
if [ "$jieba_mtime" -gt "$yesterday" ]; then
    changed_files="$changed_files jieba_userdict"
fi
if [ "$spacy_mtime" -gt "$yesterday" ]; then
    changed_files="$changed_files spacy_phrasedict"
fi

if [ -n "$changed_files" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 词典变更:${changed_files}，触发 reindex..." >> "$LOG"
    result=$(curl -sS -X POST "$REINDEX_URL" \
        -H "Content-Type: application/json" \
        -d '{}' 2>&1)
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] reindex result: $result" >> "$LOG"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 词典无变更，跳过" >> "$LOG"
fi
