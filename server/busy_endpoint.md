# /api/v1/busy — 活跃 capture 状态查询端点

## 目的

让 Kuzu 备份脚本可以在停服前精确判断 hy-memory-server 是否正在处理 capture 请求。
如果 capture 正在进行（_handle_add 正在调 LLM 做 L2/L3 提取），备份脚本应等待或跳过，
避免在 LLM 调用中途强行停服导致提取结果丢失。

## 目标文件

`hy_memory/server.py`（site-packages 中）

## 适用版本

hy-memory v1.2.21+

## 改动点

### 1. 顶部新增 import + 全局计数器

在文件开头的 import 区新增 `import threading`，并在 `_client_lock` 下方新增两个全局变量：

```python
import threading

# /api/v1/busy — 活跃 add 请求计数（线程安全）
_active_add_count = 0
_active_add_lock = threading.Lock()
```

### 2. do_GET 路由注册

在 `do_GET` 方法中，`GET /api/v1/graph/list_schemas` 路由之前添加：

```python
# GET /api/v1/busy
if path == "/api/v1/busy":
    self._handle_busy()
    return
```

### 3. _handle_add 入口/出口加计数器

在 `_handle_add` 方法开头加计数器递增（使用 `threading.Lock` 保证线程安全），
并将原有逻辑全部包裹进 `try:` 块，在 `finally` 中递减计数器：

```python
def _handle_add(self, body: Dict):
    """POST /api/v1/add"""
    global _active_add_count
    with _active_add_lock:
        _active_add_count += 1
    try:
        # ... 原有逻辑 ...
        _json_response(self, 200, result)
    finally:
        with _active_add_lock:
            _active_add_count -= 1
```

**注意**：原有代码中有多处 `return`（body 校验失败等），这些返回路径也必须在 `finally`
块中递减计数器。因此需要把所有逻辑包裹进 `try/finally` 结构中。

### 4. 新增 _handle_busy handler

在 `# Route handlers` 注释块之后，`_handle_graph_list_schemas` 方法之前，新增：

```python
def _handle_busy(self):
    """GET /api/v1/busy — 返回当前是否有 capture 正在处理"""
    busy = _active_add_count > 0
    _json_response(self, 200, {
        "busy": busy,
        "active_add_requests": _active_add_count,
    })
```

## 关键设计要点

- **线程安全**：`_handle_add` 用 `threading.Lock` 包裹计数器操作，因为 server 使用 `ThreadingHTTPServer`（多线程）
- **try/finally 保证递减**：所有返回路径（包括校验失败 return）都会触发 finally 递减，不会出现计数器泄漏
- **停服后计数器归零**：服务重启后 `_active_add_count = 0`，fresh state
- **无需持久化**：计数器是瞬态的，只反映当前内存状态，不需要写文件

## 使用方式

```bash
curl http://127.0.0.1:19527/api/v1/busy
```

返回示例：
```json
{
  "busy": true,
  "active_add_requests": 1
}
```

备份脚本中使用：
```bash
BUSY=$(curl -sS http://127.0.0.1:19527/api/v1/busy | python3 -c "import json,sys;print(json.load(sys.stdin)['busy'])")
if [ "$BUSY" = "True" ]; then
    echo "capture 正在跑，跳过备份"
    exit 0
fi
```

## 测试验证

| 场景 | 期望 busy | 期望 active_add_requests |
|------|------|------|
| 服务空闲 | false | 0 |
| capture 正在跑 | true | 1 |
| capture 跑完 | false | 0 |

三种场景均已通过 dry-run 测试验证。

## 如果不打此补丁的后果

- 备份脚本无法精确判断 capture 是否在进行，只能依赖 CPU 使用率等间接指标（不可靠）
- 可能在 LLM 提取中途停服，导致该次 L2/L3 提取结果丢失
