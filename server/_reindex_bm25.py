#!/usr/bin/env python3
"""
BM25 重建脚本（一次性迁移）

用 jieba 用户词典 + spaCy 英文分词重新索引 agent_memories_4096 中
所有 memory point 的 sparse_vectors.bm25 字段。

用法:
  cd /root/.hy-memory
  source .venv/bin/activate
  python3 _reindex_bm25.py [--dry-run] [--batch-size 50]

依赖: jieba, spacy, jieba_userdict.txt
"""

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.request
import urllib.error
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

# ── jieba + user dict ──
try:
    import jieba
except ImportError:
    print("[FATAL] jieba not installed. Run: pip install jieba")
    sys.exit(1)

USERDICT = os.environ.get("MEMORY_JIEBA_USERDICT", "/root/.hy-memory/jieba_userdict.txt")
if os.path.exists(USERDICT):
    jieba.load_userdict(USERDICT)
    print(f"[init] jieba userdict loaded: {USERDICT}")
else:
    print(f"[init] WARNING: userdict not found: {USERDICT}")

# ── spaCy (optional, English lemmatizer) ──
_nlp = None
try:
    import spacy
    _nlp = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    print("[init] spaCy en_core_web_sm loaded")
except Exception as e:
    print(f"[init] WARNING: spaCy not available, English fallback to lower: {e}")

# ── Config ──
QDRANT_URL = os.environ.get("MEMORY_VECTOR_HOST", "127.0.0.1")
QDRANT_PORT = os.environ.get("MEMORY_VECTOR_PORT", "6333")
COLLECTION = "agent_memories_4096"
BASE_URL = f"http://{QDRANT_URL}:{QDRANT_PORT}"

# batch upsert endpoint
UPSERT_URL = f"{BASE_URL}/collections/{COLLECTION}/points"
SCROLL_URL = f"{BASE_URL}/collections/{COLLECTION}/points/scroll"
WAIT_INDEX_URL = f"{BASE_URL}/collections/{COLLECTION}/points?wait=true"


def _hash_token(token: str) -> int:
    """32-bit hash of a token (consistent between index & query)."""
    h = hashlib.md5(token.encode("utf-8")).digest()[:4]
    return int.from_bytes(h, "little") & 0x7FFFFFFF  # positive int32


def tokenize(text: str) -> List[str]:
    """Tokenize text using jieba (Chinese) + spaCy (English).

    Same logic as lemmatize.py: CJK chars → jieba, else → spaCy/lower.
    """
    if not text or not isinstance(text, str):
        return []

    tokens: List[str] = []

    # ── Chinese (CJK) ──
    import re
    cjk = re.compile(r'[一-鿿㐀-䶿豈-﫿]')
    has_cjk = bool(cjk.search(text))

    if has_cjk:
        seg = jieba.cut(text, HMM=False)
        for t in seg:
            t = t.strip()
            if t and len(t) >= 1 and not re.match(r'^[\s\d\W]+$', t):
                tokens.append(t)

    # ── English ──
    en_word = re.compile(r'[a-zA-Z0-9]{2,}')
    en_words = en_word.findall(text)
    if en_words:
        if _nlp is not None:
            doc = _nlp(" ".join(en_words).lower())
            for token in doc:
                if not token.is_punct and not token.is_stop:
                    t = token.lemma_.strip()
                    if len(t) >= 2:
                        tokens.append(t)
        else:
            for w in en_words:
                tokens.append(w.lower())

    return tokens


def compute_sparse_vector(tokens: List[str]) -> Optional[Dict[str, Any]]:
    """Convert token list to Qdrant sparse vector {indices, values}.

    Uses TF weights: weight(token) = count(token) / total_tokens.
    """
    if not tokens:
        return None

    counter = Counter(tokens)
    total = len(tokens)

    indices = []
    values = []
    for token, count in counter.items():
        indices.append(_hash_token(token))
        values.append(count / total)

    return {"indices": indices, "values": values}


def scroll_all(batch_size: int = 200) -> List[Dict[str, Any]]:
    """Scroll all points from collection."""
    points: List[Dict[str, Any]] = []
    offset = None
    page = 0
    while True:
        body: Dict[str, Any] = {
            "limit": batch_size,
            "with_payload": ["search_text", "layer", "content"],
            "with_vector": False,
        }
        if offset:
            body["offset"] = offset

        req = urllib.request.Request(
            SCROLL_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = json.loads(urllib.request.urlopen(req, timeout=30).read())
        except Exception as e:
            print(f"[scroll] HTTP error at offset {offset}: {e}")
            break

        batch = resp.get("result", {}).get("points", [])
        if not batch:
            break

        points.extend(batch)
        page += 1

        next_offset = resp.get("result", {}).get("next_page_offset")
        if next_offset is None or next_offset == offset:
            break
        offset = next_offset

        if page % 5 == 0:
            print(f"  ... scrolled {len(points)} points (page {page})")

    return points


def upsert_points(points: List[Dict[str, Any]], dry_run: bool = False) -> int:
    """Upsert points with updated sparse vectors.

    Returns number of points upserted.
    """
    if dry_run:
        print(f"  [DRY RUN] would upsert {len(points)} points")
        return len(points)

    body = {"points": points}
    req = urllib.request.Request(
        UPSERT_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    req.get_method = lambda: "PUT"
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=60).read())
        status = resp.get("result", {}).get("status", "acknowledged")
        if status in ("ok", "acknowledged"):
            return len(points)
        else:
            print(f"  [upsert] unexpected status: {status}")
            return 0
    except Exception as e:
        print(f"  [upsert] HTTP error: {e}")
        return 0


def main():
    parser = argparse.ArgumentParser(description="BM25 sparse vector reindex")
    parser.add_argument("--dry-run", action="store_true", help="Tokenize but don't upsert")
    parser.add_argument("--batch-size", type=int, default=50, help="Upsert batch size")
    parser.add_argument("--limit", type=int, default=0, help="Max points to process (0=all)")
    parser.add_argument("--sample", type=int, default=5, help="Sample output count")
    args = parser.parse_args()

    t0 = time.time()

    # ── 1. Check collection exists ──
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/collections/{COLLECTION}",
            headers={"Content-Type": "application/json"},
        )
        info = json.loads(urllib.request.urlopen(req, timeout=5).read())
        total_in_col = info.get("result", {}).get("points_count", 0)
    except Exception as e:
        print(f"[FATAL] Cannot reach Qdrant at {BASE_URL}: {e}")
        sys.exit(1)

    print(f"[collection] {COLLECTION}: {total_in_col} points")
    print()

    # ── 2. Scroll all points ──
    print("[scroll] loading all points...")
    all_points = scroll_all()
    if args.limit > 0:
        all_points = all_points[:args.limit]
    print(f"[scroll] loaded {len(all_points)} points")
    print()

    # ── 3. Tokenize & build sparse vectors ──
    token_stats = Counter()
    updated: List[Dict[str, Any]] = []
    skipped_no_text = 0

    for pt in all_points:
        payload = pt.get("payload", {})
        search_text = payload.get("search_text", "") or ""
        content = payload.get("content", "") or ""

        # Use search_text as primary; fallback to content
        text = search_text if search_text.strip() else content

        tokens = tokenize(text)
        if not tokens:
            skipped_no_text += 1
            continue

        token_stats["total_tokens"] += len(tokens)
        token_stats["unique_tokens"] += len(set(tokens))

        sparse = compute_sparse_vector(tokens)
        if sparse is None:
            continue

        updated.append({
            "id": pt["id"],
            "vector": {"bm25": sparse},
            "payload": {},
        })

    print(f"[tokenize] processed {len(updated)}/{len(all_points)} points")
    print(f"  skipped (no text): {skipped_no_text}")
    print(f"  total tokens: {token_stats['total_tokens']}")
    print(f"  unique tokens: {token_stats['unique_tokens']}")
    print()

    # ── 4. Sample output ──
    if updated:
        print(f"[sample] first {min(args.sample, len(updated))} points:")
        for pt in updated[:args.sample]:
            sp = pt["vector"]["bm25"]
            ni = len(sp["indices"])
            avg_w = sum(sp["values"]) / ni if ni else 0
            print(f"  id={pt['id'][:8]}... tokens={ni} avg_weight={avg_w:.4f}")
        print()

    # ── 5. Upsert in batches ──
    if not updated:
        print("[done] no points to update")
        return

    print(f"[upsert] sending {len(updated)} points in batches of {args.batch_size}...")
    upserted = 0
    for i in range(0, len(updated), args.batch_size):
        batch = updated[i:i + args.batch_size]
        n = upsert_points(batch, dry_run=args.dry_run)
        upserted += n
        if not args.dry_run:
            time.sleep(0.1)  # throttle

    elapsed = time.time() - t0
    print()
    print(f"[done] upserted {upserted} points in {elapsed:.1f}s")
    if args.dry_run:
        print(f"[done] DRY RUN — no data written")


if __name__ == "__main__":
    main()
