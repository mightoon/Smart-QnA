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
                     支持 "Tag.prop" 格式，如 "KGIndividual.name"
  max_neighbors:     每个节点最多保留的邻居关系数（默认 10）
  excluded_relations: 排除的关系类型列表（黑名单，默认 ["contains", "references"]）
  neighbor_tags:     邻居节点可能的 Tag 列表（逗号分隔，可选）
                     不填则用 properties(m) 取所有属性，Python 中找 name
  exact_match:       是否精确匹配 VID（默认 false，用 CONTAINS 模糊匹配）
  relation_types:    关注的关系类型白名单（逗号分隔，可选）
                     不填则查所有关系类型
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


def _normalize_str_list(value: Any) -> list[str]:
    """将逗号分隔字符串或列表转为字符串列表，空则返回空列表。"""
    if not value:
        return []
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
        self.neighbor_tags = _normalize_str_list(ng_config.get("neighbor_tags"))
        self.exact_match = bool(ng_config.get("exact_match", False))
        self.relation_types = _normalize_str_list(ng_config.get("relation_types"))
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
        self.neighbor_tags = _normalize_str_list(ng_config.get("neighbor_tags"))
        self.exact_match = bool(ng_config.get("exact_match", False))
        self.relation_types = _normalize_str_list(ng_config.get("relation_types"))
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
        print(f"  [Nebula] search_by_entities 被调用, entities={entities}, top_k={top_k}")
        if not entities:
            print("  [Nebula] entities 为空，返回空结果")
            return []
        per_entity = max(1, top_k // max(1, len(entities)))
        items: list[SourceItem] = []
        async with measure("kg", "search_by_entities"):
            try:
                # Nebula 客户端是同步的，放到线程中执行
                raw_results = await asyncio.to_thread(
                    self._search_sync, entities, per_entity, top_k
                )
                print(f"  [Nebula] 查询返回 {len(raw_results)} 条结果")
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
                print(f"  [Nebula] search_by_entities 异常: {exc}")
                raise KGConnectionError(f"NebulaGraph 检索失败: {exc}") from exc
        return items

    def _search_sync(
        self, entities: list[str], per_entity: int, top_k: int
    ) -> list[tuple]:
        """同步方法：执行 NebulaGraph nGQL 查询。

        拆成两步简单查询：
        1. MATCH 查找匹配的节点（支持精确/模糊匹配）
        2. MATCH 查找每个节点的邻居关系（支持 relation_types 白名单、neighbor_tags 属性访问）
        复杂逻辑（过滤、截断、格式化）在 Python 中完成。
        """
        session = self.pool.get_session(self.username, self.password)
        results: list[tuple] = []
        try:
            session.execute(f'USE `{self.space}`')

            node_key_expr = self._build_prop_expr(self.node_key)
            excluded_set = set(self.excluded_relations)
            max_n = self.max_neighbors
            neighbor_tags = self.neighbor_tags
            relation_types = self.relation_types

            # 从 node_key 中提取 prop 名（用于 properties(m) 中查找）
            prop_name = self.node_key.split(".")[-1] if "." in self.node_key else self.node_key

            for entity in entities:
                # 第一步：查找匹配的节点
                if self.exact_match:
                    # 精确匹配 VID
                    ngql_nodes = (
                        f'MATCH (n) '
                        f'WHERE id(n) == "{entity}" '
                        f'RETURN id(n) AS vid, {node_key_expr} AS name, tags(n) AS labels '
                        f'LIMIT {per_entity}'
                    )
                else:
                    # 模糊匹配（先 toLower，失败降级 CONTAINS）
                    ngql_nodes = (
                        f'MATCH (n) '
                        f'WHERE toLower({node_key_expr}) CONTAINS toLower("{entity}") '
                        f'RETURN id(n) AS vid, {node_key_expr} AS name, tags(n) AS labels '
                        f'LIMIT {per_entity}'
                    )
                print(f"  [Nebula] 节点查询 nGQL: {ngql_nodes}")
                resp = session.execute(ngql_nodes)
                if not resp.is_succeeded() and not self.exact_match:
                    print(f"  [Nebula] toLower 查询失败: {resp.error_msg()}, 尝试普通 CONTAINS")
                    ngql_nodes = (
                        f'MATCH (n) '
                        f'WHERE {node_key_expr} CONTAINS "{entity}" '
                        f'RETURN id(n) AS vid, {node_key_expr} AS name, tags(n) AS labels '
                        f'LIMIT {per_entity}'
                    )
                    print(f"  [Nebula] 降级查询 nGQL: {ngql_nodes}")
                    resp = session.execute(ngql_nodes)
                if not resp.is_succeeded():
                    print(f"  [Nebula] 节点查询失败: {resp.error_msg()}")
                    continue

                row_count = 0
                for rec in resp:
                    row_count += 1
                    try:
                        vals = list(rec.values())
                        vid = vals[0] if len(vals) > 0 else None
                        name = vals[1] if len(vals) > 1 else ""
                        labels_raw = vals[2] if len(vals) > 2 else []
                    except Exception:
                        try:
                            vid = rec.get_value("vid")
                            name = rec.get_value("name")
                            labels_raw = rec.get_value("labels")
                        except Exception:
                            vid = None
                            name = ""
                            labels_raw = []

                    labels = self._convert_labels(labels_raw)
                    print(f"  [Nebula] 命中节点: vid={vid}, name={name}, labels={labels}")

                    if not vid:
                        continue

                    # 第二步：查找该节点的邻居关系
                    rels: list[list[str]] = []
                    try:
                        vid_str = str(vid)
                        if vid_str.startswith('"') and vid_str.endswith('"'):
                            vid_filter = vid_str
                        elif vid_str.replace("-", "").replace(".", "").isdigit():
                            vid_filter = vid_str
                        else:
                            vid_filter = f'"{vid_str}"'

                        # 关系类型过滤：白名单
                        if relation_types:
                            edge_types = "|".join(relation_types)
                            edge_pattern = f'[:{edge_types}]'
                        else:
                            edge_pattern = ''

                        # 邻居属性查询：有 neighbor_tags 则按 Tag 逐个取，否则用 properties(m)
                        if neighbor_tags:
                            # 按每个 Tag 取 prop，返回多列
                            prop_cols = ", ".join(
                                f'm.`{tag}`.`{prop_name}` AS `{tag}_{prop_name}`'
                                for tag in neighbor_tags
                            )
                            ngql_rels = (
                                f'MATCH (n)-[r{edge_pattern}]-(m) '
                                f'WHERE id(n) == {vid_filter} '
                                f'RETURN type(r) AS rtype, {prop_cols} '
                                f'LIMIT {max_n}'
                            )
                        else:
                            # 用 properties(m) 取所有属性
                            ngql_rels = (
                                f'MATCH (n)-[r{edge_pattern}]-(m) '
                                f'WHERE id(n) == {vid_filter} '
                                f'RETURN type(r) AS rtype, properties(m) AS props '
                                f'LIMIT {max_n}'
                            )

                        print(f"  [Nebula] 邻居查询 nGQL: {ngql_rels}")
                        resp2 = session.execute(ngql_rels)
                        if resp2.is_succeeded():
                            for rec2 in resp2:
                                try:
                                    vals2 = list(rec2.values())
                                    rtype = str(vals2[0]) if len(vals2) > 0 else ""
                                    if neighbor_tags:
                                        # 从多列中取第一个非空值作为 target
                                        target = ""
                                        for v in vals2[1:]:
                                            v_str = str(v) if v else ""
                                            if v_str and v_str != "None" and v_str != "__NULL__":
                                                target = v_str
                                                break
                                    else:
                                        # 从 properties map 中找 prop_name
                                        props_raw = vals2[1] if len(vals2) > 1 else {}
                                        target = self._extract_prop_from_map(props_raw, prop_name)
                                except Exception:
                                    rtype = str(rec2.get_value("rtype") or "")
                                    target = ""
                                print(f"  [Nebula] 邻居: rtype={rtype}, target={target}")
                                if rtype and rtype not in excluded_set and target and target != "None":
                                    rels.append([rtype, target])
                        else:
                            print(f"  [Nebula] 邻居查询失败: {resp2.error_msg()}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  [Nebula] 邻居查询异常: {exc}")

                    results.append((str(name) if name else "", labels, rels))
                    if len(results) >= top_k:
                        return results
                print(f"  [Nebula] 节点查询返回 {row_count} 行")
        finally:
            session.release()
        return results

    @staticmethod
    def _extract_prop_from_map(props_raw: Any, prop_name: str) -> str:
        """从 NebulaGraph properties(m) 返回的 map 中提取指定属性值。"""
        if not props_raw:
            return ""
        try:
            # ValueWrapper 可能需要 .cast() 或直接作为 dict
            if hasattr(props_raw, "cast"):
                props = props_raw.cast()
            elif isinstance(props_raw, dict):
                props = props_raw
            else:
                props = dict(props_raw)
            val = props.get(prop_name, "")
            return str(val) if val else ""
        except Exception:  # noqa: BLE001
            return ""

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
