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
  max_depth:         查询深度，1=只查直接邻居，2=查到邻居的邻居（默认 1）
  max_depth_limit:   二跳查询时每个邻居最多查几条关系（默认 5）
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

    def __init__(self, ng_config: dict, llm_client: Any = None):
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
        self.max_depth = int(ng_config.get("max_depth", 1))
        self.max_depth_limit = int(ng_config.get("max_depth_limit", 5))
        self._llm = llm_client
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
        self.max_depth = int(ng_config.get("max_depth", 1))
        self.max_depth_limit = int(ng_config.get("max_depth_limit", 5))
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
    # 检索入口
    # ------------------------------------------------------------------ #
    async def search_by_entities(
        self, entities: list[str], top_k: int = 5, query: str = ""
    ) -> list[SourceItem]:
        """根据实体列表检索图谱相关节点。

        根据 max_depth 配置选择查询深度：
        - 1: 只查直接邻居（一跳）
        - 2: 查邻居的邻居（二跳），支持简单推理
        """
        print(f"  [Nebula] search_by_entities, entities={entities}, top_k={top_k}, max_depth={self.max_depth}")
        if not entities:
            return []
        per_entity = max(1, top_k // max(1, len(entities)))
        items: list[SourceItem] = []
        async with measure("kg", "search_by_entities"):
            try:
                raw_results = await asyncio.to_thread(
                    self._search_sync, entities, per_entity, top_k
                )
                for name, labels, rels in raw_results:
                    content = self._format_record(name, labels, rels)
                    items.append(SourceItem(
                        source="kg", title=name, content=content,
                        meta={"labels": labels, "relations": rels},
                    ))
            except Exception as exc:  # noqa: BLE001
                print(f"  [Nebula] search_by_entities 异常: {exc}")
                raise KGConnectionError(f"NebulaGraph 检索失败: {exc}") from exc
        return items

    # ------------------------------------------------------------------ #
    # 同步查询核心
    # ------------------------------------------------------------------ #
    def _search_sync(
        self, entities: list[str], per_entity: int, top_k: int
    ) -> list[tuple]:
        """同步方法：执行 NebulaGraph nGQL 查询。

        一跳：查实体节点 → 查邻居关系
        二跳：在一跳基础上，对每个邻居节点再查一次邻居关系
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
            max_depth = self.max_depth
            max_depth_limit = self.max_depth_limit

            # 从 node_key 中提取 prop 名
            prop_name = self.node_key.split(".")[-1] if "." in self.node_key else self.node_key

            for entity in entities:
                # 第一步：查找匹配的节点
                if self.exact_match:
                    ngql_nodes = (
                        f'MATCH (n) '
                        f'WHERE id(n) == "{entity}" '
                        f'RETURN id(n) AS vid, {node_key_expr} AS name, tags(n) AS labels '
                        f'LIMIT {per_entity}'
                    )
                else:
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

                for rec in resp:
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

                    # 第二步：查找该节点的邻居关系（一跳，查所有关系）
                    rels = self._query_neighbors(
                        session, vid, max_n, excluded_set, prop_name
                    )

                    # 第三步（二跳）：对每个邻居节点再查一次邻居关系
                    if max_depth >= 2 and rels:
                        print(f"  [Nebula] 开始二跳查询，邻居数={len(rels)}，每邻居最多 {max_depth_limit} 条")
                        depth_rels = self._query_second_hop(
                            session, rels, max_depth_limit, excluded_set,
                            neighbor_tags, relation_types, prop_name, node_key_expr
                        )
                        # 把二跳关系追加到 rels 中
                        for d_rel in depth_rels:
                            rels.append(d_rel)

                    # 去掉一跳结果中的 mvid（只保留 [rtype, target] 用于展示）
                    display_rels = [[r[0], r[1]] for r in rels]

                    results.append((str(name) if name else "", labels, display_rels))
                    if len(results) >= top_k:
                        return results
        finally:
            session.release()
        return results

    # ------------------------------------------------------------------ #
    # 邻居查询（一跳）
    # ------------------------------------------------------------------ #
    def _query_neighbors(
        self, session, vid: Any, max_n: int, excluded_set: set,
        prop_name: str
    ) -> list[list[str]]:
        """一跳查询：查所有关系连接到的邻居，返回 [[rtype, target, mvid], ...]。

        一跳查询不过滤关系类型，查所有边。
        target 是邻居的 name 属性值（用于展示），mvid 是邻居的 VID（用于二跳查询）。
        """
        rels: list[list[str]] = []
        try:
            vid_str = str(vid)
            if vid_str.startswith('"') and vid_str.endswith('"'):
                vid_filter = vid_str
            elif vid_str.replace("-", "").replace(".", "").isdigit():
                vid_filter = vid_str
            else:
                vid_filter = f'"{vid_str}"'

            # 一跳查所有关系，不过滤关系类型
            ngql_rels = (
                f'MATCH (n)-[r]-(m) '
                f'WHERE id(n) == {vid_filter} '
                f'RETURN type(r) AS rtype, properties(m) AS props, id(m) AS mvid '
                f'LIMIT {max_n}'
            )

            print(f"  [Nebula] 一跳邻居查询 nGQL: {ngql_rels}")
            resp2 = session.execute(ngql_rels)
            if resp2.is_succeeded():
                for rec2 in resp2:
                    try:
                        vals2 = list(rec2.values())
                        rtype = str(vals2[0]) if len(vals2) > 0 else ""
                        rtype = rtype.strip('"')
                        props_raw = vals2[1] if len(vals2) > 1 else {}
                        mvid = str(vals2[2]).strip('"') if len(vals2) > 2 else ""
                        # 优先从 properties 中取 name，取不到用 VID
                        target = self._extract_prop_from_map(props_raw, prop_name)
                        if not target or target == "None":
                            target = mvid
                    except Exception:
                        rtype = str(rec2.get_value("rtype") or "")
                        target = ""
                        mvid = ""
                    print(f"  [Nebula] 一跳邻居: rtype={rtype}, target={target}, mvid={mvid}")
                    if rtype and rtype not in excluded_set and target and target != "None":
                        rels.append([rtype, target, mvid])
            else:
                print(f"  [Nebula] 一跳邻居查询失败: {resp2.error_msg()}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [Nebula] 一跳邻居查询异常: {exc}")
        return rels

    # ------------------------------------------------------------------ #
    # 二跳查询
    # ------------------------------------------------------------------ #
    def _query_second_hop(
        self, session, first_hop_rels: list[list[str]], max_depth_limit: int,
        excluded_set: set, neighbor_tags: list, relation_types: list,
        prop_name: str, node_key_expr: str
    ) -> list[list[str]]:
        """对一跳邻居节点再查一次邻居关系。

        逻辑：
        1. 用 relation_types 筛选一跳结果（没配则不筛选）
        2. 限制二跳查询数量为 max_depth_limit
        3. 对每个邻居查 neighbor_tags 指定的 Tag（没配则查所有）
        """
        depth_rels: list[list[str]] = []

        # 步骤1：用 relation_types 筛选一跳结果
        if relation_types:
            filtered = [r for r in first_hop_rels if r[0] in relation_types]
            print(f"  [Nebula] 二跳筛选: relation_types={relation_types}, 筛选后 {len(filtered)}/{len(first_hop_rels)} 条")
        else:
            filtered = first_hop_rels
            print(f"  [Nebula] 二跳筛选: relation_types 未配置，不筛选，共 {len(filtered)} 条")

        # 步骤2：限制二跳查询数量
        neighbors_to_query = filtered[:max_depth_limit]
        print(f"  [Nebula] 二跳查询: 选取前 {len(neighbors_to_query)} 个邻居（限制={max_depth_limit}）")

        # 步骤3：对每个邻居查 neighbor_tags 指定的 Tag
        for item in neighbors_to_query:
            rtype = item[0]
            target_name = item[1]  # name 属性值，用于展示
            mvid = item[2] if len(item) > 2 else ""  # VID，用于查询
            try:
                # 用 VID 做精确匹配（VID 才是 NebulaGraph 中的唯一标识）
                vid_for_query = mvid if mvid else target_name
                clean_vid = vid_for_query.strip('"').strip("'")

                # 二跳查询的关系类型过滤：用 neighbor_tags 限制目标节点的 Tag
                if neighbor_tags:
                    tag_filter = " OR ".join(
                        f'`{tag}` IN tags(m)' for tag in neighbor_tags
                    )
                    where_clause = f'WHERE id(n) == "{clean_vid}" AND ({tag_filter})'
                else:
                    where_clause = f'WHERE id(n) == "{clean_vid}"'

                ngql_rels = (
                    f'MATCH (n)-[r]-(m) '
                    f'{where_clause} '
                    f'RETURN type(r) AS rtype, properties(m) AS props, id(m) AS mvid '
                    f'LIMIT {max_depth_limit}'
                )
                print(f"  [Nebula] 二跳查询 nGQL (vid={clean_vid}, name={target_name}): {ngql_rels}")
                resp = session.execute(ngql_rels)
                if not resp.is_succeeded():
                    print(f"  [Nebula] 二跳 VID 查询失败: {resp.error_msg()}，降级为 name 匹配")
                    if neighbor_tags:
                        tag_filter = " OR ".join(
                            f'`{tag}` IN tags(m)' for tag in neighbor_tags
                        )
                        where_clause = f'WHERE toLower({node_key_expr}) CONTAINS toLower("{target_name.strip(chr(34).strip(chr(39)))}") AND ({tag_filter})'
                    else:
                        clean_name = target_name.strip('"').strip("'")
                        where_clause = f'WHERE toLower({node_key_expr}) CONTAINS toLower("{clean_name}")'
                    ngql_rels = (
                        f'MATCH (n)-[r]-(m) '
                        f'{where_clause} '
                        f'RETURN type(r) AS rtype, properties(m) AS props, id(m) AS mvid '
                        f'LIMIT {max_depth_limit}'
                    )
                    print(f"  [Nebula] 二跳降级查询 nGQL: {ngql_rels}")
                    resp = session.execute(ngql_rels)

                if resp.is_succeeded():
                    row_count = 0
                    for rec in resp:
                        row_count += 1
                        try:
                            vals = list(rec.values())
                            d_rtype = str(vals[0]) if len(vals) > 0 else ""
                            d_rtype = d_rtype.strip('"')
                            props_raw = vals[1] if len(vals) > 1 else {}
                            mvid = vals[2] if len(vals) > 2 else ""
                            d_target = self._extract_prop_from_map(props_raw, prop_name)
                            if not d_target or d_target == "None":
                                d_target = str(mvid).strip('"') if mvid else ""
                        except Exception:
                            d_rtype = ""
                            d_target = ""
                        if d_rtype and d_rtype not in excluded_set and d_target and d_target != "None":
                            depth_rels.append([d_rtype, f"{target_name} -> {d_target}"])
                            print(f"  [Nebula] 二跳邻居: {target_name} -[{d_rtype}]-> {d_target}")
                    print(f"  [Nebula] 二跳查询({target_name}) 返回 {row_count} 行")
                else:
                    print(f"  [Nebula] 二跳查询失败: {resp.error_msg()}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [Nebula] 二跳查询异常 (target={target_name}, vid={mvid}): {exc}")

        return depth_rels

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_prop_from_map(props_raw: Any, prop_name: str) -> str:
        """从 NebulaGraph properties(m) 返回的 map 中提取指定属性值。"""
        if not props_raw:
            return ""
        try:
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
    def _format_record(name: Optional[str], labels: list, rels: list) -> str:
        labels_str = "/".join(labels) if labels else "Entity"
        lines = [f"[{labels_str}] {name or ''}"]
        for rel in rels:
            if isinstance(rel, (list, tuple)) and len(rel) >= 2:
                rtype, target = rel[0], rel[1]
                if target:
                    lines.append(f"  -({rtype})-> {target}")
        return "\n".join(lines)
