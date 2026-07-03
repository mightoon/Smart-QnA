"""Neo4j 知识图谱交互逻辑。

基于抽取到的实体在图中检索相关节点及其邻居关系，
返回统一结构的来源条目。使用 neo4j 异步驱动。

配置项（kg_config）：
  uri:              Bolt 连接地址
  username/password: 认证
  database:         数据库名（默认 neo4j）
  node_key:         节点主键属性名（默认 name），用于实体匹配
  max_neighbors:    每个节点最多保留的邻居关系数（默认 10）
  excluded_relations: 排除的关系类型列表（默认 ["contains", "references"]）
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.exceptions import KGConnectionError, RetrievalError
from app.core.metrics import measure
from app.schemas.responses import SourceItem

# 默认排除的关系类型（语义价值低，通常为图谱构建时的结构性关系）
_DEFAULT_EXCLUDED_RELATIONS = ["contains", "references"]


def _normalize_excluded_relations(value: Any) -> list[str]:
    """将 excluded_relations 规范化为字符串列表。

    兼容三种输入：列表、逗号分隔字符串、None。
    """
    if not value:
        return list(_DEFAULT_EXCLUDED_RELATIONS)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    # 字符串：按逗号拆分
    return [s.strip() for s in str(value).split(",") if s.strip()]


def _build_query(node_key: str, max_neighbors: int) -> str:
    """动态构建 Cypher 查询模板。

    - 用 node_key 属性做大小写不敏感包含匹配
    - OPTIONAL MATCH 无向邻居
    - 过滤排除的关系类型
    - 过滤空目标名
    - 限制邻居数量（取前 max_neighbors 条）
    """
    return (
        "MATCH (n) "
        f"WHERE toLower(n.{node_key}) CONTAINS toLower($entity) "
        "OPTIONAL MATCH (n)-[r]-(m) "
        "WHERE NOT type(r) IN $excluded "
        "  AND coalesce(m.name, '') <> '' "
        "WITH n, collect(distinct [type(r), coalesce(m.name, '')]) AS all_rels "
        f"WITH n, all_rels[..{max_neighbors}] AS rels "
        f"RETURN n.{node_key} AS name, labels(n) AS labels, rels "
        "LIMIT $limit"
    )


class KGClient:
    """Neo4j 异步客户端封装。"""

    def __init__(self, kg_config: dict):
        self.uri = kg_config.get("uri", "bolt://localhost:7687")
        self.username = kg_config.get("username", "neo4j")
        self.password = kg_config.get("password", "")
        self.database = kg_config.get("database", "neo4j")
        self.node_key = kg_config.get("node_key", "name")
        self.max_neighbors = int(kg_config.get("max_neighbors", 10))
        self.excluded_relations = _normalize_excluded_relations(
            kg_config.get("excluded_relations", _DEFAULT_EXCLUDED_RELATIONS)
        )
        self._driver: Any = None

    # ------------------------------------------------------------------ #
    @property
    def driver(self):
        if self._driver is None:
            try:
                from neo4j import AsyncGraphDatabase
            except ImportError as exc:  # pragma: no cover
                raise RetrievalError("未安装 neo4j 依赖") from exc
            try:
                self._driver = AsyncGraphDatabase.driver(
                    self.uri, auth=(self.username, self.password)
                )
            except Exception as exc:  # noqa: BLE001
                raise KGConnectionError(f"Neo4j 驱动创建失败: {exc}") from exc
        return self._driver

    def reconfigure(self, kg_config: dict) -> None:
        self.uri = kg_config.get("uri", "bolt://localhost:7687")
        self.username = kg_config.get("username", "neo4j")
        self.password = kg_config.get("password", "")
        self.database = kg_config.get("database", "neo4j")
        self.node_key = kg_config.get("node_key", "name")
        self.max_neighbors = int(kg_config.get("max_neighbors", 10))
        self.excluded_relations = _normalize_excluded_relations(
            kg_config.get("excluded_relations", _DEFAULT_EXCLUDED_RELATIONS)
        )
        self._driver = None

    async def close(self) -> None:
        if self._driver is not None:
            try:
                await self._driver.close()
            except Exception:  # noqa: BLE001
                pass
            self._driver = None

    # ------------------------------------------------------------------ #
    # 可用性验证
    # ------------------------------------------------------------------ #
    async def validate(self) -> dict:
        if not self.uri:
            return {"ok": False, "message": "uri 未配置"}
        try:
            await self.driver.verify_connectivity()
            return {"ok": True, "message": f"连接成功: {self.uri} (db: {self.database})"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}
        finally:
            await self.close()

    # ------------------------------------------------------------------ #
    async def search_by_entities(
        self, entities: list[str], top_k: int = 5, query: str = ""
    ) -> list[SourceItem]:
        """根据实体列表检索图谱相关节点。"""
        if not entities:
            return []
        per_entity = max(1, top_k // max(1, len(entities)))
        query = _build_query(self.node_key, self.max_neighbors)
        items: list[SourceItem] = []
        async with measure("kg", "search_by_entities"):
            try:
                async with self.driver.session(database=self.database) as session:
                    for entity in entities:
                        result = await session.run(
                            query,
                            entity=entity,
                            limit=per_entity,
                            excluded=self.excluded_relations,
                        )
                        async for record in result:
                            name = record.get("name")
                            labels = record.get("labels") or []
                            rels = record.get("rels") or []
                            content = self._format_record(name, labels, rels)
                            items.append(
                                SourceItem(
                                    source="kg",
                                    title=name,
                                    content=content,
                                    meta={"labels": labels, "relations": rels},
                                )
                            )
                            if len(items) >= top_k:
                                return items
            except Exception as exc:  # noqa: BLE001
                raise KGConnectionError(f"Neo4j 检索失败: {exc}") from exc
        return items

    @staticmethod
    def _format_record(name: Optional[str], labels: list, rels: list) -> str:
        labels_str = "/".join(labels) if labels else "Entity"
        lines = [f"[{labels_str}] {name or ''}"]
        for rel in rels:
            if isinstance(rel, (list, tuple)) and len(rel) >= 2:
                rtype, target = rel[0], rel[1]
                if target:
                    lines.append(f"  -({rtype})-> {target}")
        return "\n".join(lines)
