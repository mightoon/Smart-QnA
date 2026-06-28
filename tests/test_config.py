"""配置管理逻辑测试：多配置结构、Base64 编解码、读写、脱敏、CRUD。"""

from __future__ import annotations

import base64
import json

import pytest

from app.core.config_manager import ConfigManager
from app.core.exceptions import ConfigNotFoundError


def _enc(plain: str) -> str:
    return "b64:" + base64.b64encode(plain.encode()).decode()


def test_load_decodes_sensitive(config_manager: ConfigManager):
    item = config_manager.get_active_item("llm")
    assert item["api_key"] == "super-secret-key"
    assert config_manager.get_active_item("elasticsearch")["password"] == "super-secret-key"
    assert config_manager.get_active_item("neo4j")["password"] == "super-secret-key"


def test_masked_hides_secret(config_manager: ConfigManager):
    masked = config_manager.masked()
    item = masked["llm"]["items"]["default"]
    assert item["api_key"] != "super-secret-key"
    assert "*" in item["api_key"]


def test_save_encodes_sensitive(config_manager: ConfigManager, tmp_config_file):
    item = config_manager.get_active_item("llm")
    item["api_key"] = "plain-new-key"
    config_manager.upsert_item("llm", "default", item, create=False)
    raw = json.loads(tmp_config_file.read_text(encoding="utf-8"))
    assert raw["llm"]["items"]["default"]["api_key"] == _enc("plain-new-key")
    assert config_manager.get_active_item("llm")["api_key"] == "plain-new-key"


def test_update_preserves_sensitive_when_masked(config_manager: ConfigManager):
    # 传脱敏占位，应保留原值
    config_manager.upsert_item("llm", "default", {"model": "gpt-4o", "api_key": "sk***ey"}, create=False)
    item = config_manager.get_item("llm", "default")
    assert item["model"] == "gpt-4o"
    assert item["api_key"] == "super-secret-key"


def test_set_active(config_manager: ConfigManager):
    config_manager.set_active("llm", "backup")
    assert config_manager.get_active_item("llm")["name"] == "备用"


def test_delete_item_reassigns_active(config_manager: ConfigManager):
    config_manager.set_active("llm", "backup")
    config_manager.delete_item("llm", "backup")
    # 删除生效项后应回退到剩余项
    assert "backup" not in config_manager.get_section("llm")["items"]
    assert config_manager.get_section("llm")["active"] == "default"


def test_create_item_id_collision(config_manager: ConfigManager):
    config_manager.upsert_item("llm", "gpt", {"name": "gpt", "api_key": "k1"}, create=True)
    # 再次创建同名不应冲突（id 由路由生成，这里直接测 upsert create 报错）
    with pytest.raises(Exception):
        config_manager.upsert_item("llm", "gpt", {"name": "gpt"}, create=True)


def test_plaintext_sensitive_loaded_as_is(tmp_path):
    """手动填写明文密钥（无 b64: 前缀）应原样加载。"""
    config = {
        "llm": {"active": "d", "items": {"d": {"name": "d", "api_base": "x",
                 "api_key": "sk-plain-key-123", "model": "m"}}},
        "elasticsearch": {"active": "d", "items": {"d": {"name": "d", "url": "x", "username": "",
                          "password": "plain-pw"}}},
        "neo4j": {"active": "d", "items": {"d": {"name": "d", "uri": "x", "username": "",
                  "password": "plain-pw"}}},
        "server": {"host": "0.0.0.0", "port": 8000},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    mgr = ConfigManager(config_path=path)
    assert mgr.get_active_item("llm")["api_key"] == "sk-plain-key-123"
    assert mgr.get_active_item("elasticsearch")["password"] == "plain-pw"


def test_resolve_item_merges(config_manager: ConfigManager):
    merged = config_manager.resolve_item("llm", "default", {"model": "x", "api_key": ""})
    assert merged["model"] == "x"
    assert merged["api_key"] == "super-secret-key"


def test_get_item_not_found(config_manager: ConfigManager):
    with pytest.raises(ConfigNotFoundError):
        config_manager.get_item("llm", "nope")


def test_missing_config_raises(tmp_path):
    with pytest.raises(ConfigNotFoundError):
        ConfigManager(config_path=tmp_path / "nope.json")


def test_es_field_mapping_configurable(config_manager: ConfigManager):
    """ES 字段映射应可从配置读取，未配置时用默认值。"""
    from app.retrieval.es_client import ESClient, _DEFAULT_INDEX_FIELDS

    # 已配置字段映射
    es_cfg = config_manager.get_active_item("elasticsearch")
    assert es_cfg["article_fields"] == {"body": "content", "title": "title"}
    assert es_cfg["qna_fields"] == {"body": "answer", "title": "question"}

    client = ESClient(es_cfg)
    assert client.article_fields == {"body": "content", "title": "title"}
    assert client.qna_fields == {"body": "answer", "title": "question"}

    # 未配置字段映射时应回退到默认值
    client2 = ESClient({"url": "http://localhost:9200"})
    assert client2.article_fields == _DEFAULT_INDEX_FIELDS["article"]
    assert client2.qna_fields == _DEFAULT_INDEX_FIELDS["qna"]

    # 自定义字段映射
    client3 = ESClient({
        "url": "http://localhost:9200",
        "article_fields": {"body": "text", "title": "heading"},
    })
    assert client3.article_fields == {"body": "text", "title": "heading"}
    assert client3.qna_fields == _DEFAULT_INDEX_FIELDS["qna"]  # 未配 qna 用默认


def test_kg_config_options(config_manager: ConfigManager):
    """KG 配置项（database/node_key/max_neighbors/excluded_relations）应可从配置读取，未配置用默认值。"""
    from app.retrieval.kg_client import KGClient, _DEFAULT_EXCLUDED_RELATIONS

    # 已配置
    kg_cfg = config_manager.get_active_item("neo4j")
    assert kg_cfg["database"] == "neo4j"
    assert kg_cfg["node_key"] == "name"
    assert kg_cfg["max_neighbors"] == 10
    assert kg_cfg["excluded_relations"] == ["contains", "references"]

    client = KGClient(kg_cfg)
    assert client.database == "neo4j"
    assert client.node_key == "name"
    assert client.max_neighbors == 10
    assert client.excluded_relations == ["contains", "references"]

    # 未配置时用默认值
    client2 = KGClient({"uri": "bolt://localhost:7687"})
    assert client2.database == "neo4j"
    assert client2.node_key == "name"
    assert client2.max_neighbors == 10
    assert client2.excluded_relations == _DEFAULT_EXCLUDED_RELATIONS

    # 自定义
    client3 = KGClient({
        "uri": "bolt://localhost:7687",
        "database": "mygraph",
        "node_key": "title",
        "max_neighbors": 5,
        "excluded_relations": ["contains", "belongs_to"],
    })
    assert client3.database == "mygraph"
    assert client3.node_key == "title"
    assert client3.max_neighbors == 5
    assert client3.excluded_relations == ["contains", "belongs_to"]
