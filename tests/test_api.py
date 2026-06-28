"""针对 /api/chat 与 /api/config 的测试。"""

from __future__ import annotations


def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_chat_validation_empty_query(client):
    resp = client.post("/api/chat", json={"query": ""})
    assert resp.status_code == 422


def test_chat_no_mode_blocking(client):
    """无 mode：纯对话，非流式。"""
    resp = client.post(
        "/api/chat",
        json={"query": "你好", "mode": None, "stream": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"]
    assert body["sources"] == []
    assert body["mode"] is None


def test_chat_article_blocking(client):
    resp = client.post("/api/chat", json={"query": "文章", "mode": "article", "stream": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "article"
    assert len(body["sources"]) == 1
    assert body["sources"][0]["source"] == "article"


def test_chat_qna_blocking(client):
    resp = client.post("/api/chat", json={"query": "问答", "mode": "qna", "stream": False})
    assert resp.status_code == 200
    assert resp.json()["sources"][0]["source"] == "qna"


def test_chat_kg_blocking(client):
    resp = client.post("/api/chat", json={"query": "图谱", "mode": "kg", "stream": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "kg"
    assert len(body["entities"]) > 0
    assert body["sources"][0]["source"] == "kg"


def test_chat_all_blocking(client):
    resp = client.post("/api/chat", json={"query": "全部", "mode": "all", "stream": False, "top_k": 3})
    assert resp.status_code == 200
    sources = [s["source"] for s in resp.json()["sources"]]
    assert "article" in sources
    assert "qna" in sources
    assert "kg" in sources


def test_chat_merge_blocking(client):
    """merge 模式：四路检索 + rerank 融合，sources 应被截断到 top_k。"""
    resp = client.post("/api/chat", json={"query": "融合", "mode": "merge", "stream": False, "top_k": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "merge"
    assert len(body["sources"]) <= 2


def test_chat_vec_blocking(client):
    """vec 模式：向量检索（多路主题拆分），sources 应来自 vec。"""
    resp = client.post("/api/chat", json={"query": "向量数据库的工作原理", "mode": "vec", "stream": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "vec"
    assert len(body["sources"]) >= 1
    assert body["sources"][0]["source"] == "vec"


def test_chat_invalid_mode(client):
    resp = client.post("/api/chat", json={"query": "x", "mode": "unknown", "stream": False})
    assert resp.status_code == 422


def test_chat_stream_sse(client):
    with client.stream("POST", "/api/chat",
                        json={"query": "流式测试", "mode": "all", "stream": True}) as resp:
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        text = b"".join(resp.iter_bytes()).decode("utf-8")
        assert "event: meta" in text
        assert "event: token" in text
        assert "event: done" in text


# ---------------- 配置管理 ----------------
def test_config_get_masked(client):
    resp = client.get("/api/config")
    assert resp.status_code == 200
    cfg = resp.json()
    # items 结构 + 脱敏
    item = cfg["llm"]["items"]["default"]
    assert item["api_key"]
    assert "*" in item["api_key"]


def test_config_create_item(client):
    resp = client.post("/api/config/llm/items", json={
        "name": "新模型", "api_base": "https://x/v1", "api_key": "sk-new",
        "model": "m", "entity_model": "m", "temperature": 0.5, "max_tokens": 100,
    })
    assert resp.status_code == 200
    sec = resp.json()
    assert "新模型" in [it["name"] for it in sec["items"].values()]


def test_config_update_item_preserves_sensitive(client):
    # 修改非敏感字段，敏感字段留空/脱敏时应保留原值
    resp = client.put("/api/config/llm/items/default", json={"model": "gpt-4o", "api_key": ""})
    assert resp.status_code == 200
    # 重新读取脱敏快照，应仍显示脱敏（说明保留了原值）
    cfg = client.get("/api/config").json()
    assert cfg["llm"]["items"]["default"]["model"] == "gpt-4o"
    assert "*" in cfg["llm"]["items"]["default"]["api_key"]


def test_config_set_active(client):
    resp = client.put("/api/config/llm/active", json={"id": "backup"})
    assert resp.status_code == 200
    assert resp.json()["active"] == "backup"


def test_config_delete_item(client):
    resp = client.delete("/api/config/llm/items/backup")
    assert resp.status_code == 200
    assert "backup" not in resp.json()["items"]


def test_config_unknown_section(client):
    resp = client.post("/api/config/unknown/items", json={"name": "x"})
    assert resp.status_code == 404


def test_metrics_endpoint(client):
    """指标接口应返回结构化数据。"""
    # 先发一个 chat 请求产生指标
    client.post("/api/chat", json={"query": "hi", "mode": None, "stream": False})
    resp = client.get("/api/metrics?range_hours=1")
    assert resp.status_code == 200
    body = resp.json()
    assert "summary" in body
    assert "timeseries" in body
    assert body["summary"]["total"] >= 1
