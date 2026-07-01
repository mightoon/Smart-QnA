"""NebulaGraph 知识图谱交互逻辑。

与 kg_client.py（Neo4j）接口一致，基于抽取到的实体在图中检索相关节点
及其邻居关系，返回统一结构的来源条目。

NebulaGraph 的 Python 客户端（nebula3-python）是同步的，
本封装通过 asyncio.to_thread() 将阻塞调用转为异步。

配置项（ng_config）：
  host:              graphd 地址（host:port，默认 localhost:9669）
  username/password: 认证
  space:             图空间名（NebulaGraph 用 space 做数据隔离）
  node_key:          节点主键属性名（默认 name），用于实体匹配
  max_neighbors:     每个节点最多保留的邻居关系数（默认 10）
  excluded_relations: 排除的关系类型列表
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from app.core.exceptions import KGConnectionError, RetrievalError
from app.core.metrics import measure
from app.schemas.responses import SourceItem

_DEFAULT_EXCLUDED_RELATIONS = ["contains", "references"]


def _normalize_excluded_relations(value: Any) -> list[str]:
    if not value:
        return list(_DEFAULT_EXCLUDED_RELATIONS)
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


class NGClient:
    """NebulaGraph 异步客户端封装（底层为同步，通过 to_thread 转异步）。"""

    def __init__(self, ng_config: dict):
        self.host = ng_config.get("host", "localhost:9669")
        self.username = ng_config.get("username", "root")
        self.password = ng_config.get("password", "nebula")
        self.space = ng_config.get("space", "default")
        self.node_key = ng_config.get("node_key", "name")
        self.max_neighbors = int(ng_config.get("max_neighbors", 10))
        self.excluded_relations = _normalize_excluded_relations(
            ng_config.get("excluded_relations", _DEFAULT_EXCLUDED_RELATIONS)
        )
        self._pool: Any = None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _build_prop_expr(node_key: str, prefix: str = "n") -> str:
        """将 node_key 构建为 NebulaGraph 属性访问表达式。

        支持两种格式：
        - "name"        -> n.`name`        （不带 Tag）
        - "Person.name" -> n.`Person`.`name` （带 Tag，NebulaGraph 要求）
        """
        parts = node_key.split(".")
        if len(parts) == 2:
            tag, prop = parts
            return f"{prefix}.`{tag}`.`{prop}`"
        return f"{prefix}.`{node_key}`"

    # ------------------------------------------------------------------ #
    @property
    def pool(self):
        if self._pool is None:
            try:
                from nebula3.gclient.net import ConnectionPool
                from nebula3.Config import Config as NebulaConfig
            except ImportError as exc:
                raise RetrievalError("未安装 nebula3-python 依赖") from exc
            try:
                # 解析 host:port
                parts = self.host.split(":")
                host = parts[0]
                port = int(parts[1]) if len(parts) > 1 else 9669

                config = NebulaConfig()
                config.max_connection_pool_size = 10
                config.timeout = 10000  # ms

                pool = ConnectionPool()
                ok = pool.init([(host, port)], config)
                if not ok:
                    raise KGConnectionError(f"NebulaGraph 连接池初始化失败: {self.host}")
                self._pool = pool
            except KGConnectionError:
                raise
            except Exception as exc:
                raise KGConnectionError(f"NebulaGraph 连接池创建失败: {exc}") from exc
        return self._pool

    def reconfigure(self, ng_config: dict) -> None:
        self.host = ng_config.get("host", "localhost:9669")
        self.username = ng_config.get("username", "root")
        self.password = ng_config.get("password", "nebula")
        self.space = ng_config.get("space", "default")
        self.node_key = ng_config.get("node_key", "name")
        self.max_neighbors = int(ng_config.get("max_neighbors", 10))
        self.excluded_relations = _normalize_excluded_relations(
            ng_config.get("excluded_relations", _DEFAULT_EXCLUDED_RELATIONS)
        )
        self._pool = None

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await asyncio.to_thread(self._pool.close)
            except Exception:  # noqa: BLE001
                pass
            self._pool = None

    # ------------------------------------------------------------------ #
    # 可用性验证
    # ------------------------------------------------------------------ #
    async def validate(self) -> dict:
        if not self.host:
            return {"ok": False, "message": "host 未配置"}
        try:
            # 尝试获取 session 并执行简单查询
            await asyncio.to_thread(self._test_connection)
            return {"ok": True, "message": f"连接成功: {self.host} (space: {self.space})"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"验证失败: {exc}"}
        finally:
            await self.close()

    def _test_connection(self) -> None:
        """同步方法：测试连接和 space 可用性。"""
        session = self.pool.get_session(self.username, self.password)
        try:
            session.execute(f'USE `{self.space}`')
            session.execute('YIELD 1')
        finally:
            session.release()

    # ------------------------------------------------------------------ #
    async def search_by_entities(
        self, entities: list[str], top_k: int = 5
    ) -> list[SourceItem]:
        """根据实体列表检索图谱相关节点。"""
        if not entities:
            return []
        per_entity = max(1, top_k // max(1, len(entities)))
        items: list[SourceItem] = []
        async with measure("kg", "search_by_entities"):
            try:
                # Nebula 客户端是同步的，放到线程中执行
                raw_results = await asyncio.to_thread(
                    self._search_sync, entities, per_entity, top_k
                )
                for name, labels, rels in raw_results:
                    content = self._format_record(name, labels, rels)
                    items.append(
                        SourceItem(
                            source="kg",
                            title=name,
                            content=content,
                            meta={"labels": labels, "relations": rels},
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                raise KGConnectionError(f"NebulaGraph 检索失败: {exc}") from exc
        return items

    def _search_sync(
        self, entities: list[str], per_entity: int, top_k: int
    ) -> list[tuple]:
        """同步方法：执行 NebulaGraph nGQL 查询。"""
        session = self.pool.get_session(self.username, self.password)
        results: list[tuple] = []
        try:
            session.execute(f'USE `{self.space}`')

            excluded_str = ",".join(f'"{r}"' for r in self.excluded_relations)
            node_key = self._build_prop_expr(self.node_key)
            max_n = self.max_neighbors

            for entity in entities:
                # 使用 nGQL MATCH 语法查询（NebulaGraph 3.x 支持）
                # 按节点属性做大小写不敏感包含匹配
                ngql = (
                    f'USE `{self.space}`; '
                    f'MATCH (n) '
                    f'WHERE toLower({node_key}) CONTAINS toLower("{entity}") '
                    f'OPTIONAL MATCH (n)-[r]-(m) '
                    f'WHERE NOT type(r) IN [{excluded_str}] '
                    f'  AND coalesce(m.{self._build_prop_expr(self.node_key, prefix="m")}, "") <> "" '
                    f'WITH n, collect(distinct [type(r), coalesce(m.{self._build_prop_expr(self.node_key, prefix="m")}, "")]) AS all_rels '
                    f'WITH n, all_rels[..{max_n}] AS rels '
                    f'RETURN {node_key} AS name, tags(n) AS labels, rels '
                    f'LIMIT {per_entity}'
                )
                resp = session.execute(ngql)
                if not resp.is_succeeded():
                    continue

                for rec in resp:
                    name = rec.value_at(0) if rec.column_size() > 0 else None
                    labels_raw = rec.value_at(1) if rec.column_size() > 1 else []
                    rels_raw = rec.value_at(2) if rec.column_size() > 2 else []

                    # NebulaGraph 返回的 labels 和 rels 可能是特殊类型，转为 list
                    labels = self._convert_labels(labels_raw)
                    rels = self._convert_rels(rels_raw)

                    results.append((str(name) if name else "", labels, rels))
                    if len(results) >= top_k:
                        return results
        finally:
            session.release()
        return results

    @staticmethod
    def _convert_labels(raw: Any) -> list[str]:
        """将 NebulaGraph 返回的 labels 转为字符串列表。"""
        if not raw:
            return []
        try:
            return [str(x) for x in raw]
        except Exception:  # noqa: BLE001
            return [str(raw)]

    @staticmethod
    def _convert_rels(raw: Any) -> list[list[str]]:
        """将 NebulaGraph 返回的关系列表转为 [[type, target], ...]。"""
        if not raw:
            return []
        result = []
        try:
            for item in raw:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    result.append([str(item[0]), str(item[1])])
        except Exception:  # noqa: BLE001
            pass
        return result

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
