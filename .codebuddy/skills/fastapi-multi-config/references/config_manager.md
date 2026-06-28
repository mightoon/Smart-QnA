# ConfigManager 完整实现

## 敏感字段遍历

针对 items 结构，遍历所有配置项的敏感字段：

```python
ITEM_SECTIONS = {
    "llm": ("api_key",),
    "elasticsearch": ("password",),
    "neo4j": ("password",),
}

@staticmethod
def _apply_to_sensitive(config: dict, fn) -> None:
    for section, sensitive_keys in ITEM_SECTIONS.items():
        sec = config.get(section)
        if not isinstance(sec, dict):
            continue
        items = sec.get("items")
        if not isinstance(items, dict):
            continue
        for item in items.values():
            if not isinstance(item, dict):
                continue
            for key in sensitive_keys:
                if key in item and isinstance(item[key], str):
                    item[key] = fn(item[key])
```

## b64: 前缀编解码

```python
_B64_PREFIX = "b64:"

def _b64encode(value: str) -> str:
    if not value:
        return ""
    if value.startswith(_B64_PREFIX):
        return value  # 已编码不重复编码
    return _B64_PREFIX + base64.b64encode(value.encode("utf-8")).decode("ascii")

def _b64decode(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(_B64_PREFIX):
        return value  # 明文原样返回
    raw = value[len(_B64_PREFIX):]
    return base64.b64decode(raw.encode("ascii")).decode("utf-8")
```

## 修改保留原值

更新配置项时，敏感字段为空或脱敏占位（含 *）时保留原值：

```python
def _is_masked(value) -> bool:
    if value is None:
        return True
    s = str(value)
    return not s or "*" in s

def _merge_sensitive(self, section, stored, fields):
    merged = deepcopy(stored)
    sensitive = ITEM_SECTIONS.get(section, ())
    for k, v in fields.items():
        if k in sensitive and _is_masked(v):
            continue  # 保留原值
        merged[k] = v
    return merged
```

## CRUD 方法

```python
def upsert_item(self, section, item_id, fields, create=False):
    # create=True: 不允许已存在
    # create=False: 合并敏感字段后更新
    # 首次创建自动设为 active

def delete_item(self, section, item_id):
    # 删除生效项后自动回退到剩余项

def set_active(self, section, item_id):
    # 切换生效配置项

def get_active_item(self, section) -> dict:
    # 返回当前生效配置项（明文）
```

## 线程安全

使用 `threading.RLock` 保护所有读写操作。写操作通过临时文件 + os.replace 保证原子性。

## 脱敏

```python
def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]
```
