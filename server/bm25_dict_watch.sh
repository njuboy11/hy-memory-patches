#!/bin/bash
# 每天凌晨 3:30 全量重建 BM25 稀疏向量（用 jieba + spaCy 自定义词典）
set -e

REINDEX_URL="http://127.0.0.1:19527/api/v1/reindex_bm25"
LOG="/var/log/bm25_dict_watch.log"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 定时 reindex 开始..." >> "$LOG"
result=$(curl -sS -X POST "$REINDEX_URL" \
    -H "Content-Type: application/json" \
    -d '{}' 2>&1)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] result: $result" >> "$LOG"
