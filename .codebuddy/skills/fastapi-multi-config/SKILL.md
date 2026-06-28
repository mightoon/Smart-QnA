---
name: fastapi-multi-config
description: >-
  FastAPI 多配置管理技能。提供多份命名配置（items + active 结构）、敏感字段 Base64 加解密（b64: 前缀）、
  配置 CRUD 接口、生效切换、可用性验证、脱敏展示的完整实现模式。适用于需要管理多个外部服务连接
  （数据库、缓存、LLM、检索引擎等）配置的 FastAPI 项目。当用户提到配置管理、多环境配置、
  敏感字段加密、配置切换验证时触发。
---

# FastAPI 多配置管理

## 概述

本技能提供 FastAPI 服务中管理多个外部工具（LLM / 数据库 / 检索引擎等）连接配置的完整模式：每个工具支持多份命名配置，可在线增删改查、切换生效项，敏感字段自动加解密与脱敏，切换/修改前验证可用性。

## 何时使用

- FastAPI 项目需要管理多个外部服务的连接配置
- 需要多环境/多实例配置切换（如多个 LLM、多个数据库）
- 需要敏感字段（API Key、密码）的安全存储与脱敏展示
- 需要配置变更前的可用性验证

## 配置数据模型

### 多配置结构
每个工具段包含 `active`（生效项 ID）与 `items`（配置项字典）：

```json
{
  "llm": {
    "active": "deepseek",
    "items": {
      "deepseek": { "name": "DeepSeek", "api_base": "...", "api_key": "b64:...", "model": "..." },
      "openai":   { "name": "OpenAI",   "api_base": "...", "api_key": "b64:...", "model": "..." }
    }
  }
}
```

### 敏感字段处理

**核心设计：`b64:` 前缀标记法**

- **编码（落盘）**：`"b64:" + base64(明文)`，已编码不重复编码
- **解码（读取）**：仅对带 `b64:` 前缀的值解码；无前缀视为明文原样使用
- **脱敏（展示）**：`前2位 + **** + 后2位`
- **修改保留**：更新时若敏感字段为空或含 `*`，保留原值

前缀标记法的关键优势：用户手动编辑配置文件填写明文也能正常工作，不会误解码。

## 完整实现模式

详细的配置管理器实现（线程安全读写、CRUD、合并保留原值、脱敏）与 API 路由设计，参见：
- `references/config_manager.md` —— ConfigManager 完整实现与 CRUD 方法
- `references/config_api.md` —— 配置管理 REST API 设计与验证逻辑

## API 接口设计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 读取全部配置（脱敏） |
| POST | `/api/config/{section}/items` | 新增配置项 |
| PUT | `/api/config/{section}/items/{id}` | 修改配置项（敏感字段保留原值） |
| DELETE | `/api/config/{section}/items/{id}` | 删除配置项（删除生效项自动回退） |
| PUT | `/api/config/{section}/active` | 切换生效配置项 |
| POST | `/api/config/{section}/validate` | 验证可用性 |

## 依赖注入与重建

通过 `lru_cache` 复用按 active 配置构造的客户端单例；配置更新后调用 `rebuild_clients()` 清空缓存重建：

```python
@lru_cache(maxsize=1)
def get_llm_client():
    cfg = get_config_manager().get_active_item("llm")
    return LLMClient(cfg)

def rebuild_clients():
    for fn in (get_llm_client, ...):
        clear = getattr(fn, "cache_clear", None)
        if callable(clear):
            clear()
```

## 实现清单

1. 定义 `ITEM_SECTIONS` 映射（段名 → 敏感字段名列表）
2. 实现 ConfigManager：多配置读写、b64: 前缀加解密、CRUD、脱敏、合并保留原值
3. 实现配置 CRUD REST API
4. 实现各客户端的 `validate()` 方法
5. 实现 lru_cache 单例依赖注入 + rebuild 机制
6. 路由层通过模块属性访问依赖，便于测试 monkeypatch
