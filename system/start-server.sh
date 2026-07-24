#!/bin/bash
# hy-memory server launcher — 从 KeyVault 读 key 设 env, exec server
set -e

KV="/root/.openclaw/keys.b64.json"
if [ ! -f "$KV" ]; then
    echo "[start-server] FATAL: $KV not found" >&2
    exit 1
fi

# 用 python 解码 base64（避免在 shell 里展开触发脱敏）
eval "$(python3 - <<'PY'
import json, base64
kv = json.load(open('/root/.openclaw/keys.b64.json'))
sf = base64.b64decode(kv['providers']['siliconflow']['value_b64']).decode()
mm = base64.b64decode(kv['providers']['minimax_key2']['value_b64']).decode()
# shell 导出: 单引号转义
def shquote(s):
    return "'" + s.replace("'", "'\\''") + "'"
print(f'export MEMORY_EMBEDDER_PROVIDER="openai"')
print(f'export MEMORY_EMBEDDER_MODEL="Qwen/Qwen3-Embedding-8B"')
print(f'export MEMORY_EMBEDDER_API_KEY={shquote(sf)}')
print(f'export MEMORY_EMBEDDER_BASE_URL="https://api.siliconflow.cn/v1"')
print(f'export MEMORY_EMBEDDING_DIMS="4096"')
print(f'export MEMORY_EMBEDDER_TIMEOUT="15"')
print(f'export MEMORY_EMBEDDER_MAX_RETRIES="2"')
print(f'export MEMORY_LLM_PROVIDER="openai"')
print(f'export MEMORY_ENABLE_SUMMARY="true"')
print(f'export HY_MEMORY_THINKING_MODE="disabled"')
print(f'export MEMORY_LLM_MODEL="MiniMax-M3"')
print(f'export MEMORY_LLM_API_KEY={shquote(mm)}')
print(f'export MEMORY_LLM_BASE_URL="https://api.minimaxi.com/v1"')
print(f'export MEMORY_LLM_TEMPERATURE="0.1"')
print(f'export MEMORY_LLM_MAX_TOKENS="2048"')
print(f'export MEMORY_VECTOR_STORE="qdrant"')
print(f'export MEMORY_RERANKER_ENABLED="true"')
print(f'export MEMORY_RERANKER_URL="https://api.siliconflow.cn/v1/rerank"')
print(f'export MEMORY_RERANKER_API_KEY={shquote(sf)}')
print(f'export MEMORY_RERANKER_MODEL="Qwen/Qwen3-Reranker-8B"')
print(f'export MEMORY_RERANKER_TIMEOUT="10000"')
print(f'export MEMORY_RERANKER_MAX_CANDIDATES="50"')
print(f'export MEMORY_RERANKER_MIN_SCORE="0.6"')
print(f'export MEMORY_VECTOR_STORE="qdrant"')
print(f'export MEMORY_VECTOR_HOST="127.0.0.1"')
print(f'export MEMORY_VECTOR_PORT="6333"')
print(f'export MEMORY_MODE="ultra"')
print(f'export MEMORY_ENABLE_GRAPH="true"')
print(f'export MEMORY_ENABLE_AGENT="true"')
print(f'export HY_MEMORY_READER="simple_hybrid"')
print(f'export MEMORY_JIEBA_USERDICT="/root/.hy-memory/jieba_userdict.txt"')
print(f'export MEMORY_READER_VEC_MULT="3"')
print(f'export MEMORY_READER_BM25_MULT="1.5"')
PY
)"

# 启动
exec /root/.hy-memory/.venv/bin/python -m hy_memory.server \
    --port 19527 --host 127.0.0.1