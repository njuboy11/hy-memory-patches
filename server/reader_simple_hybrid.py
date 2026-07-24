"""
SimpleHybridReadPipeline — Vec + BM25 + Reranker (per-path).

Three independent paths (Profile / Normal / Proactive), each:
  - Vec recall: Qdrant/Kuzu vector search (3x overfetch)
  - BM25 recall: Qdrant keyword search (1.5x overfetch)
  - Union + dedup + Reranker + truncate to path limit

Design principles:
  - No score fusion (no 0.6/0.4, no RRF)
  - Per-path independent reranker (no cross-path pollution)
  - Three reranker calls via asyncio.gather (total latency ≈ max single)
  - Profile path retains dual-source (Qdrant L0 + Kuzu L6) from Legacy
"""

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime
import asyncio
import math
import os
import logging

from .base import ReadPipeline, ReadRequest, ReadResponse, PipelineContext
from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..models.memory import MemoryNode, MemoryLayer, MemoryStatus
from ..data.vector_store import create_vector_store
from ..data.vector_store_base import VectorStoreBase
from ..data.graph_store_base import GraphStoreBase
from ._retrieval.lemmatize import lemmatize_for_bm25
from ._retrieval.reranker import RerankerConfig, RerankerService
from ._retrieval.intention import recall_intentions
from ._retrieval.evolution import expand_evolution_chains

logger = logging.getLogger(__name__)

# ── config defaults (env-overridable) ──

_VEC_MULT = float(os.environ.get("MEMORY_READER_VEC_MULT", "3"))
_BM25_MULT = float(os.environ.get("MEMORY_READER_BM25_MULT", "1.5"))

# ── layer definitions ──

_PROFILE_VDB_LAYERS = [MemoryLayer.L0_BASIC_INFO]
_PROFILE_GRAPH_LAYERS = [MemoryLayer.L6_SCHEMA]
_NORMAL_VDB_LAYERS = [
    MemoryLayer.L2_FACT,
    MemoryLayer.L3_SUMMARY,
    MemoryLayer.L4_IDENTITY,
]
_PROACTIVE_LAYERS = [MemoryLayer.L7_INTENTION]

_PROFILE_LAYER_VALS = {MemoryLayer.L0_BASIC_INFO.value, MemoryLayer.L6_SCHEMA.value}


class SimpleHybridReadPipeline(ReadPipeline):
    """Simple hybrid reader: Vec + BM25 + Reranker for each path."""

    VERSION = "simple_hybrid"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: Optional[EmbedService] = None,
        vector_store: Optional[VectorStoreBase] = None,
        graph_store: Optional[GraphStoreBase] = None,
        cache: Any = None,
    ):
        self.config = config
        self._embed_service = embed_service
        self._external_vector_store = vector_store
        self._graph_store = graph_store
        self._cache = cache
        self._vector_store: Optional[VectorStoreBase] = None
        self._vector_store_initialized = False
        self._reranker_config = RerankerConfig()
        self._reranker: Optional[RerankerService] = (
            RerankerService(self._reranker_config)
            if self._reranker_config.enabled
            else None
        )
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if self._external_vector_store and getattr(self._external_vector_store, "_client", None):
            self._vector_store_initialized = True
        self._initialized = True

    @property
    def embed_service(self) -> EmbedService:
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        return self._embed_service

    async def _get_vector_store(self) -> VectorStoreBase:
        if self._vector_store is None:
            self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if not self._vector_store_initialized:
            await self._vector_store.initialize()
            self._vector_store_initialized = True
        return self._vector_store

    # ──────────────────────────────────────────────
    # Main pipeline
    # ──────────────────────────────────────────────

    async def read(
        self,
        request: ReadRequest,
        ctx: Optional[PipelineContext] = None,
        tracer: Any = None,
    ) -> ReadResponse:
        start_time = datetime.now()
        response = ReadResponse()

        try:
            if not request.query:
                response.error_code = 400
                response.error_message = "query is required"
                return response

            # ── Stage 1: Embed + Lemmatize ──
            query_embedding = request.query_embedding
            if not query_embedding:
                query_embedding = await self.embed_service.embed(request.query)

            query_lemmatized = lemmatize_for_bm25(request.query)

            # ── Stage 2: Build isolation params ──
            iso = self._build_isolation(request)
            if iso.get("error_msg"):
                response.error_code = 400
                response.error_message = iso["error_msg"]
                return response

            vector_store = await self._get_vector_store()

            # limits
            profile_limit = request.profile_limit if request.profile_limit > 0 else 5
            intention_limit = request.intention_limit if request.intention_limit > 0 else 3
            final_limit = request.limit if request.limit > 0 else 8

            vec_pool = math.ceil(final_limit * _VEC_MULT)
            bm25_pool = math.ceil(final_limit * _BM25_MULT)
            profile_vec_pool = math.ceil(profile_limit * _VEC_MULT)
            profile_bm25_pool = math.ceil(profile_limit * _BM25_MULT)
            intent_vec_pool = math.ceil(intention_limit * _VEC_MULT)
            intent_bm25_pool = math.ceil(intention_limit * _BM25_MULT)

            # common search args
            search_kwargs = {
                "isolation_key": iso.get("isolation_key", ""),
                "isolation_keys": iso.get("isolation_keys"),
                "user_ids": iso.get("user_ids"),
                "agent_ids": iso.get("agent_ids"),
            }

            kw_user_id = request.user_id or (request.user_ids[0] if request.user_ids else "")
            kw_agent_ids = request.agent_ids if request.agent_ids else (
                [request.agent_id] if request.agent_id else None
            )

            _recall_status = [MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED]

            graph_user_id = request.user_id or (request.user_ids[0] if request.user_ids else "")
            has_graph = self._graph_store is not None

            # ── Stage 3: Three parallel paths ──
            profile_task = self._search_profile(
                request, vector_store, query_embedding, query_lemmatized,
                profile_vec_pool, profile_bm25_pool, profile_limit,
                search_kwargs, kw_user_id, kw_agent_ids, _recall_status,
                graph_user_id, has_graph,
            )
            normal_task = self._search_normal(
                request, vector_store, query_embedding, query_lemmatized,
                vec_pool, bm25_pool, final_limit,
                search_kwargs, kw_user_id, kw_agent_ids, _recall_status,
            )
            proactive_task = self._search_proactive(
                request, vector_store, query_embedding, query_lemmatized,
                intent_vec_pool, intent_bm25_pool, intention_limit,
                search_kwargs, kw_user_id, kw_agent_ids, _recall_status,
            )

            raw_profile, raw_normal, raw_proactive = await asyncio.gather(
                profile_task, normal_task, proactive_task,
                return_exceptions=True,
            )

            # ── Re-raise real exceptions ──
            for label, r in [("profile", raw_profile), ("normal", raw_normal), ("proactive", raw_proactive)]:
                if isinstance(r, Exception):
                    logger.error(f"[simple_hybrid] {label} path failed: {r}", exc_info=True)

            profile_results = raw_profile if isinstance(raw_profile, list) else []
            normal_results = raw_normal if isinstance(raw_normal, list) else []
            proactive_results = raw_proactive if isinstance(raw_proactive, list) else []

            # ── Stage 4: Expand evolution chains (VDB items with node objects) ──
            all_expandable = []
            for item in profile_results + normal_results + proactive_results:
                if item.get("node") is not None:
                    all_expandable.append(item)

            if all_expandable:
                expanded = await expand_evolution_chains(vector_store, all_expandable)
                # Replace items in their respective lists
                expanded_map = {e["node_id"]: e for e in expanded if "node_id" in e}
                for lst in [profile_results, normal_results, proactive_results]:
                    for i, item in enumerate(lst):
                        nid = item.get("node_id", "")
                        if nid in expanded_map:
                            lst[i] = expanded_map[nid]

            # ── Stage 5: Assemble response ──
            final_results = profile_results + normal_results + proactive_results

            for item in final_results:
                node: Optional[MemoryNode] = item.get("node")
                node_id = item.get("node_id", "")

                if node:
                    content = node.content
                    layer = node.layer.value
                    speculate = getattr(node, "speculate", None)
                    source_raw_memory_id = getattr(node, "source_raw_memory_id", None)
                    tags = list(node.tags) if getattr(node, "tags", None) else []
                    memory_at = int(node.memory_at.timestamp()) if node.memory_at else None
                    gmt_created = int(node.gmt_created.timestamp()) if node.gmt_created else None
                    owner = getattr(node, "owner", None)
                    access_count = getattr(node, "access_count", 0)
                    agent_id = getattr(node, "agent_id", "") or ""
                else:
                    content = item.get("content", "")
                    layer = item.get("layer", "l6_schema")
                    speculate = None
                    source_raw_memory_id = None
                    tags = []
                    memory_at = None
                    gmt_created = None
                    owner = None
                    access_count = 0
                    agent_id = ""

                display_score = item.get("score", 0.0)

                mem_entry = {
                    "memory_id": node_id,
                    "content": content,
                    "layer": layer,
                    "score": float(display_score),
                    "agent_id": agent_id,
                    "access_count": access_count,
                    "owner": owner,
                    "speculate": speculate,
                    "source_raw_memory_id": source_raw_memory_id,
                    "tags": tags,
                    "memory_at": memory_at,
                    "gmt_created": gmt_created,
                    "source": item.get("source", ""),
                }

                response.memories.append(mem_entry)

            response.total_found = len(final_results)
            response.extra["reader"] = self.VERSION
            response.extra["channels"] = {
                "profile": len(profile_results),
                "normal": len(normal_results),
                "proactive": len(proactive_results),
            }
            response.success = True

            # Per-memory source breakdown
            src_counts = {"vdb": 0, "bm25_only": 0, "profile_forward": 0, "profile_l6": 0}
            for item in final_results:
                s = item.get("source", "?")
                src_counts[s] = src_counts.get(s, 0) + 1

            logger.info(
                f"[read/simple_hybrid] "
                f"profile={len(profile_results)} normal={len(normal_results)} "
                f"proactive={len(proactive_results)} returned={len(final_results)} "
                f"sources={dict(src_counts)} "
                f"query='{request.query[:80]}'"
            )

        except Exception as e:
            logger.error(f"SimpleHybridReadPipeline.read failed: {e}", exc_info=True)
            response.error_code = 500
            response.error_message = str(e)

        response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
        return response

    # ──────────────────────────────────────────────
    # Path: Profile (Qdrant L0 + Kuzu L6 + BM25 L0)
    # ──────────────────────────────────────────────

    async def _search_profile(
        self, request, vector_store, query_embedding, query_lemmatized,
        vec_pool, bm25_pool, profile_limit,
        search_kwargs, kw_user_id, kw_agent_ids, recall_status,
        graph_user_id, has_graph,
    ) -> List[Dict[str, Any]]:
        if profile_limit == 0:
            return []

        tasks = []

        # Vec: Qdrant L0
        tasks.append(vector_store.search(
            query_embedding=query_embedding,
            layers=_PROFILE_VDB_LAYERS,
            limit=vec_pool,
            status_filter=recall_status,
            only_latest=False,
            score_threshold=request.profile_min_score if request.profile_min_score > 0 else 0.3,
            **search_kwargs,
        ))

        # Vec: Kuzu L6
        if has_graph:
            tasks.append(self._graph_store.vector_search(
                query_embedding=query_embedding,
                isolation_key="",
                layers=[MemoryLayer.L6_SCHEMA.value],
                limit=vec_pool,
                score_threshold=request.profile_min_score if request.profile_min_score > 0 else 0.3,
                user_id=graph_user_id,
            ))

        # BM25: Qdrant L0 (Kuzu has no BM25)
        tasks.append(vector_store.keyword_search(
            query=query_lemmatized,
            top_k=bm25_pool,
            user_id=kw_user_id,
            agent_ids=kw_agent_ids,
            layers=_PROFILE_VDB_LAYERS,
            status_filter=recall_status,
            only_latest=False,
        ))

        all_raw = await asyncio.gather(*tasks, return_exceptions=True)

        vec_hits: List[Dict[str, Any]] = []
        bm25_hits: List[Dict[str, Any]] = []

        # First N-1 results are vec, last is BM25
        for i, r in enumerate(all_raw):
            if isinstance(r, Exception):
                logger.warning(f"[simple_hybrid] profile recall[{i}] failed: {r}")
                continue
            if isinstance(r, list):
                if i < len(tasks) - 1:
                    vec_hits.extend(r)
                else:
                    bm25_hits = r

        return await self._merge_rerank_evolve(
            request.query, vec_hits, bm25_hits, profile_limit, vector_store
        )

    # ──────────────────────────────────────────────
    # Path: Normal (Qdrant L2/L3/L4 + BM25)
    # ──────────────────────────────────────────────

    async def _search_normal(
        self, request, vector_store, query_embedding, query_lemmatized,
        vec_pool, bm25_pool, final_limit,
        search_kwargs, kw_user_id, kw_agent_ids, recall_status,
    ) -> List[Dict[str, Any]]:
        results = await asyncio.gather(
            vector_store.search(
                query_embedding=query_embedding,
                layers=_NORMAL_VDB_LAYERS,
                limit=vec_pool,
                status_filter=recall_status,
                only_latest=False,
                score_threshold=request.min_score if request.min_score > 0 else 0.3,
                **search_kwargs,
            ),
            vector_store.keyword_search(
                query=query_lemmatized,
                top_k=bm25_pool,
                user_id=kw_user_id,
                agent_ids=kw_agent_ids,
                layers=_NORMAL_VDB_LAYERS,
                status_filter=recall_status,
                only_latest=False,
            ),
            return_exceptions=True,
        )

        vec_hits = results[0] if isinstance(results[0], list) else []
        if isinstance(results[0], Exception):
            logger.warning(f"[simple_hybrid] normal vec failed: {results[0]}")

        bm25_hits = results[1] if isinstance(results[1], list) else []
        if isinstance(results[1], Exception):
            logger.warning(f"[simple_hybrid] normal bm25 failed: {results[1]}")

        return await self._merge_rerank_evolve(
            request.query, vec_hits, bm25_hits, final_limit, vector_store
        )

    # ──────────────────────────────────────────────
    # Path: Proactive (Kuzu L7 + BM25 Qdrant L7)
    # ──────────────────────────────────────────────

    async def _search_proactive(
        self, request, vector_store, query_embedding, query_lemmatized,
        vec_pool, bm25_pool, intention_limit,
        search_kwargs, kw_user_id, kw_agent_ids, recall_status,
    ) -> List[Dict[str, Any]]:
        if intention_limit <= 0:
            return []

        tasks = []

        # Vec: Kz L7 via recall_intentions (handles expired L7 → L2_FACT)
        tasks.append(recall_intentions(
            vector_store,
            query_embedding,
            user_ids=search_kwargs.get("user_ids"),
            agent_ids=search_kwargs.get("agent_ids"),
            limit=vec_pool,
        ))

        # BM25: Qdrant L7
        tasks.append(vector_store.keyword_search(
            query=query_lemmatized,
            top_k=bm25_pool,
            user_id=kw_user_id,
            agent_ids=kw_agent_ids,
            layers=_PROACTIVE_LAYERS,
            status_filter=recall_status,
            only_latest=False,
        ))

        all_raw = await asyncio.gather(*tasks, return_exceptions=True)

        vec_hits = all_raw[0] if isinstance(all_raw[0], list) else []
        if isinstance(all_raw[0], Exception):
            logger.warning(f"[simple_hybrid] proactive vec failed: {all_raw[0]}")

        bm25_hits = all_raw[1] if isinstance(all_raw[1], list) else []
        if isinstance(all_raw[1], Exception):
            logger.warning(f"[simple_hybrid] proactive bm25 failed: {all_raw[1]}")

        return await self._merge_rerank_evolve(
            request.query, vec_hits, bm25_hits, intention_limit, vector_store
        )

    # ──────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────

    async def _merge_rerank_evolve(
        self, query: str,
        vec_hits: List[Dict[str, Any]],
        bm25_hits: List[Dict[str, Any]],
        top_k: int,
        vector_store: Any,
    ) -> List[Dict[str, Any]]:
        """Merge vec + BM25 → dedup by node_id → rerank → top_k.

        On reranker failure/timeout: fallback to vec-only
        (BM25 candidates excluded from fallback per design).
        """

        # ── Merge + dedup (vec preferred) ──
        seen: Dict[str, int] = {}
        merged: List[Dict[str, Any]] = []

        bm25_total = len(bm25_hits)
        bm25_new = 0
        bm25_duped = 0

        for hit in vec_hits:
            nid = hit.get("node_id", "")
            if nid and nid not in seen:
                seen[nid] = len(merged)
                hit["source"] = "vec"
                merged.append(hit)

        for hit in bm25_hits:
            nid = hit.get("node_id", "")
            if nid and nid not in seen:
                seen[nid] = len(merged)
                hit["source"] = "bm25"
                merged.append(hit)
                bm25_new += 1
            elif nid:
                bm25_duped += 1

        if not merged:
            return []

        # ── Reranker (with vec-only fallback on failure) ──
        reranker_ok = False
        if self._reranker is not None and len(merged) >= 2:
            merged = await self._reranker.rerank(query, merged, top_k)
            # reranker sets _rerank_score on success; absent on failure/fallback
            reranker_ok = any("_rerank_score" in h for h in merged)
            # log BM25 recall stats on success too
            if reranker_ok:
                bm25_final = sum(1 for h in merged if h.get("source") == "bm25")
                logger.info(
                    f"[simple_hybrid] reranker OK; "
                    f"bm25_recalled={bm25_total} new={bm25_new} duped={bm25_duped} "
                    f"bm25_in_final={bm25_final} top_k={top_k}"
                )

        if not reranker_ok:
            # Vec-only fallback (BM25 excluded per design)
            logger.info(
                f"[simple_hybrid] reranker {'failed' if len(merged) >= 2 else 'skipped'}; "
                f"bm25_recalled={bm25_total} new={bm25_new} duped={bm25_duped} "
                f"fallback to vec-only (top_k={top_k})"
            )
            vec_hits.sort(key=lambda x: x.get("score", 0.0), reverse=True)
            return vec_hits[:top_k]

        return merged

    def _build_isolation(self, request: ReadRequest) -> Dict[str, Any]:
        """Build isolation parameters (same logic as legacy/hybrid_v2)."""
        user_ids = request.user_ids if request.user_ids else ([request.user_id] if request.user_id else [])
        agent_ids = request.agent_ids
        session_ids = request.session_ids

        if agent_ids and len(agent_ids) > 1 and session_ids and len(session_ids) > 1:
            return {
                "error_msg": (
                    "Cannot specify multiple agent_ids and multiple session_ids simultaneously."
                )
            }

        isolation_key = ""
        isolation_keys = None
        search_user_ids = None
        search_agent_ids = None

        if not agent_ids and not session_ids:
            if request.agent_id:
                keys = [MemoryNode.build_isolation_key(u, request.agent_id or "default") for u in user_ids]
                if keys and len(keys) == 1:
                    isolation_key = keys[0]
                else:
                    isolation_keys = keys
            else:
                search_user_ids = user_ids if user_ids else None
        elif agent_ids and not session_ids:
            search_user_ids = user_ids if user_ids else None
            search_agent_ids = agent_ids
        else:
            effective_agent_ids = agent_ids if agent_ids else [request.agent_id or "default"]
            isolation_keys = [
                MemoryNode.build_isolation_key(u, a, s)
                for u in (user_ids or ["default"])
                for a in effective_agent_ids
                for s in session_ids
            ]

        return {
            "isolation_key": isolation_key,
            "isolation_keys": isolation_keys,
            "user_ids": search_user_ids,
            "agent_ids": search_agent_ids,
        }

    async def close(self) -> None:
        logger.info("SimpleHybridReadPipeline closed")
