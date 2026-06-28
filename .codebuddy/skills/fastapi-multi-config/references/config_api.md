# 配置管理 API 设计

## REST 接口

### 新增配置项
```python
@router.post("/config/{section}/items")
async def create_item(section: str, body: dict):
    _check_section(section)
    item_id = _gen_item_id(manager, section, body.get("name") or "item")
    manager.upsert_item(section, item_id, body, create=True)
    deps.rebuild_clients()
    return manager.masked_section(section)
```

ID 由 name 生成 slug（`re.sub(r"[^a-zA-Z0-9_-]+", "-", name).lower()`），冲突时追加 `-2`、`-3`。

### 修改配置项
```python
@router.put("/config/{section}/items/{item_id}")
async def update_item(section: str, item_id: str, body: dict):
    manager.upsert_item(section, item_id, body, create=False)
    deps.rebuild_clients()
```

### 切换生效
```python
@router.put("/config/{section}/active")
async def set_active(section: str, payload: SetActiveRequest):
    manager.set_active(section, payload.id)
    deps.rebuild_clients()
```

### 验证可用性
```python
@router.post("/config/{section}/validate")
async def validate_item(section: str, body: dict):
    # body 含 id → 验证已存储项（用真实凭据）
    # body 含内联字段 → 验证草稿
    item_id = body.get("id")
    fields = {k: v for k, v in body.items() if k != "id"}
    if item_id:
        config = manager.resolve_item(section, item_id, fields)
    else:
        config = fields
    result = await _validate_section(section, config)
    return result
```

## 各段验证逻辑

| 段 | 验证方式 |
|---|---|
| llm | 发起极小请求（max_tokens=8），成功即可用 |
| elasticsearch | `client.info()` 获取集群信息 |
| neo4j | `driver.verify_connectivity()` |
| rerank | 最小 rerank 请求（query="ping", documents=["test"]） |
| embedding | `embed("ping")` 向量化请求 |
| milvus | `list_collections()` |

返回 `{ok: bool, message: string}`。
