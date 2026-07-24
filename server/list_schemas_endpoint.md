# list_schemas_endpoint — GET /api/v1/graph/list_schemas 端点

## 目的

让 OpenClaw agent 可以通过 HTTP 直接查询 Kuzu 图数据库中的所有 L6 Schema 节点，
避免因为 Kuzu 单进程独占锁而需要停服才能查询。

## 目标文件

`hy_memory/server.py`（site-packages 中）

## 适用版本

hy-memory v1.2.21+

## 改动点

### 1. do_GET 路由注册

在 `do_GET` 方法中，找到 `/api/v1/metrics` 路由之后的代码位置（通常是 `/api/v1/memories/:id` 正则匹配之前）。

在 `GET /api/v1/memories/:id` 之前添加：

```python
# GET /api/v1/graph/list_schemas
if path == "/api/v1/graph/list_schemas":
    self._handle_graph_list_schemas()
    return
```

### 2. 新增 _handle_graph_list_schemas 方法

在 `# Route handlers` 注释块之后，`_handle_health_liveness` 方法之前，新增一个方法：

```python
def _handle_graph_list_schemas(self):
    """GET /api/v1/graph/list_schemas — 列出所有 L6 Schema 节点。

    不经过 client（避免触发异步/loop），直接用 graph_store 的底层 _execute
    同步查 Kuzu Memory 表中 layer='l6_schema' 的节点。
    """
    try:
        client = _get_client()
        gs = getattr(client, '_graph_store', None)
        if gs is None or not getattr(gs, '_available', False):
            _json_response(self, 503, {
                "error": "graph_store not available",
                "schemas": [],
                "count": 0,
            })
            return

        rows = gs._execute(
            "MATCH (s:Memory) WHERE s.layer='l6_schema' "
            "RETURN s.node_id, s.content, s.confidence, s.created_at "
            "ORDER BY s.created_at"
        )

        schemas = []
        for row in rows:
            schemas.append({
                "node_id": row[0],
                "content": row[1],
                "confidence": row[2],
                "created_at": str(row[3]) if row[3] is not None else None,
            })

        _json_response(self, 200, {
            "schemas": schemas,
            "count": len(schemas),
        })
    except Exception as e:
        logger.error(f"[server] graph/list_schemas error: {e}", exc_info=True)
        _json_response(self, 500, {"error": str(e)})
```

## 关键设计要点

- **不走 client 异步层**：`_graph_store` 是同步对象，`_execute` 是同步方法，直接在 http handler 线程中执行
- **Kuzu 查询放在 server 进程内**：因为 Kuzu 是单进程独占锁，只有 server 进程可以读 Kuzu 文件
- **graph_store 不可用时的降级**：返回 503 + 空 list，不抛异常
- **查询只扫 Memory 表**：`WHERE s.layer='l6_schema'` 限制只返回 Schema 节点

## 使用方式

```bash
curl http://127.0.0.1:19527/api/v1/graph/list_schemas
```

返回示例：
```json
{
  "schemas": [
    {
      "node_id": "cf70cfb4-...",
      "content": "When designing and operating infrastructure systems...",
      "confidence": 0.8,
      "created_at": "2026-07-23 23:29:23.748291"
    }
  ],
  "count": 7
}
```

## 如果不打此补丁的后果

- 无法通过 HTTP 直接查询 L6 Schema 数量
- 每次查 Schema 需要停服（systemctl stop → Python 读 Kuzu → systemctl start），累计约 10-15 秒服务中断
- 不影响 Auto Recall 和 digest 的正常运行（这是辅助查询接口）

## 部署后的验证

```bash
# 重启服务
systemctl --user restart hy-memory-server.service

# 测试新端点
curl -sS http://127.0.0.1:19527/api/v1/graph/list_schemas

# 确认服务健康
curl -sS http://127.0.0.1:19527/healthz
```
