"""配置管理：本地 JSON 文件读写与 Base64 加解密。

配置结构：
  每个工具段（llm / elasticsearch / neo4j）支持多份命名配置（items），
  通过 active 指定当前生效的那一份。敏感字段（api_key / password）落盘时
  以 `b64:` 前缀 + Base64 编码存储，读取时自动解码，对外脱敏。
  server 段为普通扁平配置。
"""

from __future__ import annotations

import base64
import json
import os
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Optional

from app.core.exceptions import ConfigError, ConfigNotFoundError

_B64_PREFIX = "b64:"

# 各工具段：使用 items 结构，并列出该段中的敏感字段名。
ITEM_SECTIONS: dict[str, tuple[str, ...]] = {
    "llm": ("api_key",),
    "elasticsearch": ("password",),
    "neo4j": ("password",),
    "nebula": ("password",),
    "rerank": ("api_key",),
    "embedding": ("api_key",),
    "milvus": (),
}

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "config.json"


def _b64encode(value: str) -> str:
    if not value:
        return ""
    if value.startswith(_B64_PREFIX):
        return value
    return _B64_PREFIX + base64.b64encode(value.encode("utf-8")).decode("ascii")


def _b64decode(value: str) -> str:
    """解码敏感字段；仅对带 `b64:` 前缀的值解码，否则视为明文原样返回。"""
    if not value:
        return ""
    if not value.startswith(_B64_PREFIX):
        return value
    raw = value[len(_B64_PREFIX):]
    try:
        return base64.b64decode(raw.encode("ascii")).decode("utf-8")
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"配置字段 Base64 解码失败: {exc}") from exc


def _is_masked(value: Any) -> bool:
    """判断值是否为脱敏占位（含 *）或空，更新时用于保留原值。"""
    if value is None:
        return True
    s = str(value)
    return not s or "*" in s


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "****"
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


class ConfigManager:
    """线程安全的配置管理器。"""

    def __init__(self, config_path: Optional[Path | str] = None):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._lock = threading.RLock()
        self._config: dict = {}
        self.reload()

    # ------------------------------------------------------------------ #
    # 敏感字段遍历（针对 items 结构）
    # ------------------------------------------------------------------ #
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

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def reload(self) -> dict:
        with self._lock:
            if not self.config_path.exists():
                raise ConfigNotFoundError(f"配置文件不存在: {self.config_path}")
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"配置文件 JSON 解析失败: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError("配置文件根必须是 JSON 对象")
            decoded = deepcopy(raw)
            self._apply_to_sensitive(decoded, _b64decode)
            self._config = decoded
            return deepcopy(self._config)

    def save(self, config: dict) -> None:
        """将配置写入磁盘（敏感字段 Base64 编码）。"""
        with self._lock:
            if not isinstance(config, dict):
                raise ConfigError("配置必须是 JSON 对象")
            encoded = deepcopy(config)
            self._apply_to_sensitive(encoded, _b64encode)
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.config_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(encoded, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.config_path)
            decoded = deepcopy(encoded)
            self._apply_to_sensitive(decoded, _b64decode)
            self._config = decoded

    # ------------------------------------------------------------------ #
    # 访问
    # ------------------------------------------------------------------ #
    def get(self) -> dict:
        with self._lock:
            return deepcopy(self._config)

    def get_section(self, section: str) -> dict:
        with self._lock:
            value = self._config.get(section)
            if value is None:
                raise ConfigNotFoundError(f"配置段不存在: {section}")
            return deepcopy(value)

    def get_active_item(self, section: str) -> dict:
        """返回当前生效配置项（明文）。"""
        with self._lock:
            sec = self._config.get(section)
            if not isinstance(sec, dict):
                raise ConfigNotFoundError(f"配置段不存在: {section}")
            items = sec.get("items") or {}
            active_id = sec.get("active")
            if active_id not in items:
                if items:
                    active_id = next(iter(items))
                else:
                    raise ConfigNotFoundError(f"段 {section} 没有可用配置项")
            return deepcopy(items[active_id])

    def get_item(self, section: str, item_id: str) -> dict:
        with self._lock:
            sec = self._config.get(section)
            items = (sec or {}).get("items") or {}
            if item_id not in items:
                raise ConfigNotFoundError(f"配置项不存在: {section}/{item_id}")
            return deepcopy(items[item_id])

    def masked(self) -> dict:
        """返回敏感字段已脱敏的配置快照。"""
        with self._lock:
            snapshot = deepcopy(self._config)
            self._apply_to_sensitive(snapshot, _mask)
            return snapshot

    def masked_section(self, section: str) -> dict:
        snap = self.masked()
        return snap.get(section)

    # ------------------------------------------------------------------ #
    # 配置项 CRUD
    # ------------------------------------------------------------------ #
    def _merge_sensitive(self, section: str, stored: dict, fields: dict) -> dict:
        merged = deepcopy(stored)
        sensitive = ITEM_SECTIONS.get(section, ())
        for k, v in fields.items():
            if k in sensitive and _is_masked(v):
                continue
            merged[k] = v
        return merged

    def resolve_item(self, section: str, item_id: str, fields: dict) -> dict:
        """合并：以已存储项为基础，叠加 fields（敏感字段为空/脱敏时保留原值）。"""
        stored = self.get_item(section, item_id)
        return self._merge_sensitive(section, stored, fields)

    def upsert_item(self, section: str, item_id: str, fields: dict, create: bool = False) -> None:
        with self._lock:
            sec = self._config.setdefault(section, {"active": None, "items": {}})
            items = sec.setdefault("items", {})
            if create:
                if item_id in items:
                    raise ConfigError(f"配置项已存在: {item_id}")
                merged = deepcopy(fields)
            else:
                if item_id not in items:
                    raise ConfigNotFoundError(f"配置项不存在: {item_id}")
                merged = self._merge_sensitive(section, items[item_id], fields)
            if not merged.get("name"):
                merged["name"] = item_id
            items[item_id] = merged
            if not sec.get("active"):
                sec["active"] = item_id
            self.save(self._config)

    def delete_item(self, section: str, item_id: str) -> None:
        with self._lock:
            sec = self._config.get(section)
            if not isinstance(sec, dict):
                raise ConfigNotFoundError(f"配置段不存在: {section}")
            items = sec.get("items") or {}
            if item_id not in items:
                raise ConfigNotFoundError(f"配置项不存在: {item_id}")
            del items[item_id]
            if sec.get("active") == item_id:
                sec["active"] = next(iter(items), None)
            self.save(self._config)

    def set_active(self, section: str, item_id: str) -> None:
        with self._lock:
            sec = self._config.get(section)
            if not isinstance(sec, dict):
                raise ConfigNotFoundError(f"配置段不存在: {section}")
            items = sec.get("items") or {}
            if item_id not in items:
                raise ConfigNotFoundError(f"配置项不存在: {item_id}")
            sec["active"] = item_id
            self.save(self._config)

    def update_server(self, fields: dict) -> None:
        with self._lock:
            existing = self._config.get("server", {})
            existing.update(fields)
            self._config["server"] = existing
            self.save(self._config)
