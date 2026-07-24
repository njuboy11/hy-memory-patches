"""
External Reranker — SiliconFlow Qwen/Qwen3-Reranker-8B.

Env:
  MEMORY_RERANKER_ENABLED=true
  MEMORY_RERANKER_URL          (default https://api.siliconflow.cn/v1/rerank)
  MEMORY_RERANKER_API_KEY      (required)
  MEMORY_RERANKER_MODEL        (default Qwen/Qwen3-Reranker-8B)
  MEMORY_RERANKER_TIMEOUT      (default 2000 ms)
  MEMORY_RERANKER_MAX_CANDIDATES (default 50)
  MEMORY_RERANKER_MIN_SCORE    (default 0.1)

Fallback: all scores below min_score → restore original vector scores.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)


class RerankerConfig:

    def __init__(self) -> None:
        self.enabled: bool = (
            os.getenv("MEMORY_RERANKER_ENABLED", "false").lower() == "true"
        )
        self.api_url: str = os.getenv(
            "MEMORY_RERANKER_URL", "https://api.siliconflow.cn/v1/rerank"
        )
        self.api_key: str = os.getenv("MEMORY_RERANKER_API_KEY", "")
        self.model: str = os.getenv(
            "MEMORY_RERANKER_MODEL", "Qwen/Qwen3-Reranker-8B"
        )
        self.timeout: int = int(os.getenv("MEMORY_RERANKER_TIMEOUT", "2000"))
        self.max_candidates: int = int(
            os.getenv("MEMORY_RERANKER_MAX_CANDIDATES", "50")
        )
        self.min_score: float = float(
            os.getenv("MEMORY_RERANKER_MIN_SCORE", "0.1")
        )


class RerankerService:

    def __init__(self, config: RerankerConfig) -> None:
        self._config = config
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._config.timeout / 1000,
                headers={
                    "Authorization": f"Bearer {self._config.api_key}",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def rerank(
        self, query: str, hits: List[Dict[str, Any]], top_k: int
    ) -> List[Dict[str, Any]]:
        if not hits:
            return hits

        # ── 收集 content ──
        documents: List[str] = []
        valid_indices: List[int] = []
        for i, hit in enumerate(hits):
            node = hit.get("node")
            content = (node.content if node and node.content else "").strip()
            if content:
                documents.append(content)
                valid_indices.append(i)

        if len(documents) < 2:
            return self._fallback_sort(hits, top_k)

        # ── 保存原向量分（覆盖前备份）──
        for h in hits:
            if "_score_original" not in h:
                h["_score_original"] = h.get("score", 0.0)

        # ── 调用 reranker ──
        try:
            client = await self._get_client()
            body: Dict[str, Any] = {
                "model": self._config.model,
                "query": query,
                "documents": documents[: self._config.max_candidates],
            }
            response = await client.post(self._config.api_url, json=body)
            response.raise_for_status()
            result = response.json()
            scores = self._parse_scores(result, len(documents))
        except Exception as e:
            logger.warning(f"[reranker] call failed: {e}; fallback to original")
            return self._fallback_sort(hits, top_k, use_original=True)

        # ── 写回分数 ──
        for vi, score in zip(valid_indices, scores):
            hits[vi]["_rerank_score"] = score
            hits[vi]["score"] = float(score)

        # ── min_score 过滤 ──
        hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        filtered = [h for h in hits if h.get("score", 0.0) >= self._config.min_score]
        if not filtered:
            logger.debug(
                f"[reranker] all {len(hits)} candidates below "
                f"min_score={self._config.min_score}, returning empty"
            )
            return []
        return filtered[:top_k]

    # ------------------------------------------------------------------ #
    # private helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _fallback_sort(
        hits: List[Dict[str, Any]], top_k: int, use_original: bool = False,
    ) -> List[Dict[str, Any]]:
        if use_original:
            hits.sort(
                key=lambda x: x.get("_score_original", x.get("score", 0.0)),
                reverse=True,
            )
            for h in hits:
                h["score"] = h.get("_score_original", h.get("score", 0.0))
        else:
            hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
        return hits[:top_k]

    @staticmethod
    def _parse_scores(result: Dict[str, Any], expected_count: int) -> List[float]:
        if "results" in result:
            score_map = {
                r["index"]: float(r.get("relevance_score", 0.0))
                for r in result["results"]
            }
            return [score_map.get(i, 0.0) for i in range(expected_count)]
        if "data" in result:
            return [float(s) for s in result["data"]]
        return [0.0] * expected_count
