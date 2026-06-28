#!/usr/bin/env python
"""Smart QnA 服务客户端演示脚本。

依次调用 /api/chat 的各种参数模式，端到端打印每一步执行情况：
  1. 无 mode（纯对话）
  2. article（ES 文章检索）
  3. qna（ES 问答检索）
  4. kg（知识图谱检索）
  5. vec（向量检索）
  6. all（三路并行检索）
  7. merge（四路并行 + rerank 融合）
  8. 流式 SSE 模式

用法：
  python client_demo.py                          # 默认 http://localhost:8000
  python client_demo.py --base-url http://x:8000 # 指定服务地址
  python client_demo.py --skip kg                 # 跳过某些 case
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Optional

try:
    import httpx
except ImportError:
    print("请先安装 httpx: pip install httpx")
    sys.exit(1)

# ---------------------------------------------------------------------- #
# 样例问题
# ---------------------------------------------------------------------- #
CASES = [
    {"name": "纯对话（无 mode）", "body": {"query": "你好，请用一句话介绍你自己", "mode": None, "stream": False}},
    {"name": "article 文章检索", "body": {"query": "什么是 RAG 检索增强生成", "mode": "article", "stream": False, "top_k": 3}},
    {"name": "qna 问答检索", "body": {"query": "如何部署 FastAPI 应用", "mode": "qna", "stream": False, "top_k": 3}},
    {"name": "kg 知识图谱检索", "body": {"query": "DeepSeek 和 GPT-4 的区别", "mode": "kg", "stream": False, "top_k": 3}},
    {"name": "vec 向量检索", "body": {"query": "向量数据库的工作原理", "mode": "vec", "stream": False, "top_k": 5}},
    {"name": "all 三路并行", "body": {"query": "大模型微调的最佳实践", "mode": "all", "stream": False, "top_k": 3}},
    {"name": "merge 四路融合", "body": {"query": "知识图谱与向量检索的优缺点", "mode": "merge", "stream": False, "top_k": 3}},
    {"name": "流式 SSE（all）", "body": {"query": "解释一下 embedding 向量化", "mode": "all", "stream": True, "top_k": 3}},
]


# ---------------------------------------------------------------------- #
# 工具函数
# ---------------------------------------------------------------------- #
def banner(title: str) -> None:
    width = 70
    print("\n" + "=" * width)
    print(f"  {title}")
    print("=" * width)


def step(msg: str) -> None:
    print(f"  [{_now()}] {msg}")


def _now() -> str:
    return time.strftime("%H:%M:%S", time.localtime()) + f".{int((time.time() % 1) * 1000):03d}"


def fmt_duration(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f}ms"
    return f"{ms / 1000:.2f}s"


def print_sources(sources: list) -> None:
    if not sources:
        print("    （无检索来源）")
        return
    for i, s in enumerate(sources, 1):
        score = f"  score={s['score']:.4f}" if s.get("score") is not None else ""
        title = f"  标题={s['title']}" if s.get("title") else ""
        print(f"    [{i}] 来源={s['source']}{title}{score}")
        content = s.get("content", "").replace("\n", "\n        ")
        preview = content[:120] + ("..." if len(content) > 120 else "")
        print(f"        {preview}")


# ---------------------------------------------------------------------- #
# 非流式调用
# ---------------------------------------------------------------------- #
def call_blocking(client: httpx.Client, base_url: str, case: dict) -> None:
    banner(f"CASE: {case['name']}（非流式）")
    step(f"请求参数: {json.dumps(case['body'], ensure_ascii=False)}")
    step(f"POST {base_url}/api/chat")

    t0 = time.monotonic()
    try:
        resp = client.post(f"{base_url}/api/chat", json=case["body"], timeout=60)
        elapsed = (time.monotonic() - t0) * 1000
    except Exception as exc:
        step(f"✗ 请求失败: {exc}")
        return

    step(f"收到响应: HTTP {resp.status_code}  耗时 {fmt_duration(elapsed)}")

    if resp.status_code != 200:
        step(f"✗ 错误响应: {resp.text[:200]}")
        return

    data = resp.json()
    step(f"mode={data.get('mode')}  entities={data.get('entities')}")

    step("检索来源:")
    print_sources(data.get("sources", []))

    step("模型回答:")
    answer = data.get("answer", "")
    for line in answer.split("\n"):
        print(f"    {line}")

    step(f"完成  端到端耗时 {fmt_duration(elapsed)}")


# ---------------------------------------------------------------------- #
# 流式调用（SSE）
# ---------------------------------------------------------------------- #
def call_streaming(client: httpx.Client, base_url: str, case: dict) -> None:
    banner(f"CASE: {case['name']}（流式 SSE）")
    step(f"请求参数: {json.dumps(case['body'], ensure_ascii=False)}")
    step(f"POST {base_url}/api/chat  (text/event-stream)")

    t0 = time.monotonic()
    token_count = 0
    answer_buf = []
    sources = []
    entities = []

    try:
        with client.stream("POST", f"{base_url}/api/chat", json=case["body"], timeout=120) as resp:
            step(f"连接建立: HTTP {resp.status_code}  content-type={resp.headers.get('content-type', '?')}")
            if resp.status_code != 200:
                body = b"".join(resp.iter_bytes()).decode("utf-8", errors="replace")
                step(f"✗ 错误响应: {body[:200]}")
                return

            t_first = None
            buf = ""

            for chunk in resp.iter_text():
                buf += chunk
                # SSE 事件以双换行分隔
                while "\n\n" in buf:
                    block, buf = buf.split("\n\n", 1)
                    event, data = _parse_sse_block(block)

                    if event == "meta":
                        meta_t = (time.monotonic() - t0) * 1000
                        step(f"[meta 事件] 耗时 {fmt_duration(meta_t)}  mode={data.get('mode')}")
                        entities = data.get("entities", [])
                        sources = data.get("sources", [])
                        if entities:
                            print(f"    抽取实体: {entities}")
                        step("检索来源:")
                        print_sources(sources)
                        step("开始接收 token...")

                    elif event == "token":
                        if t_first is None:
                            t_first = time.monotonic() - t0
                            step(f"首 token 延迟: {fmt_duration(t_first * 1000)}")
                        content = data.get("content", "")
                        answer_buf.append(content)
                        token_count += 1
                        print(content, end="", flush=True)

                    elif event == "error":
                        print()  # 换行
                        step(f"✗ [error 事件] {data}")

                    elif event == "done":
                        total = (time.monotonic() - t0) * 1000
                        print()  # 换行结束 token 流
                        step(f"[done 事件]")
                        step(f"token 数: {token_count}")
                        step(f"首 token 延迟: {fmt_duration(t_first * 1000) if t_first else 'N/A'}")
                        step(f"总耗时: {fmt_duration(total)}")

    except Exception as exc:
        print()
        step(f"✗ 流式连接异常: {exc}")


def _parse_sse_block(block: str) -> tuple[str, dict]:
    event = "message"
    data_str = ""
    for line in block.split("\n"):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data_str += line[5:].strip()
    if not data_str:
        return event, {}
    try:
        return event, json.loads(data_str)
    except json.JSONDecodeError:
        return event, {"raw": data_str}


# ---------------------------------------------------------------------- #
# 健康检查
# ---------------------------------------------------------------------- #
def check_health(client: httpx.Client, base_url: str) -> bool:
    banner("健康检查")
    step(f"GET {base_url}/api/health")
    try:
        resp = client.get(f"{base_url}/api/health", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            step(f"✓ 服务正常  status={data['status']}  version={data.get('version')}")
            return True
        else:
            step(f"✗ 服务异常: HTTP {resp.status_code}")
            return False
    except Exception as exc:
        step(f"✗ 连接失败: {exc}")
        return False


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description="Smart QnA 客户端演示")
    parser.add_argument("--base-url", default="http://localhost:8000", help="服务地址")
    parser.add_argument("--skip", nargs="*", default=[], help="跳过的 case 名称关键词")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")
    print(f"Smart QnA 客户端演示  →  {base_url}")

    with httpx.Client() as client:
        # 健康检查
        if not check_health(client, base_url):
            print("\n服务不可用，请确认服务已启动。")
            sys.exit(1)

        # 逐个执行 case
        passed, failed = 0, 0
        for case in CASES:
            # 跳过逻辑
            if any(kw.lower() in case["name"].lower() for kw in args.skip):
                banner(f"SKIP: {case['name']}")
                continue

            try:
                if case["body"].get("stream"):
                    call_streaming(client, base_url, case)
                else:
                    call_blocking(client, base_url, case)
                passed += 1
            except KeyboardInterrupt:
                print("\n\n用户中断。")
                break
            except Exception as exc:
                print(f"\n  ✗ case 异常: {exc}")
                failed += 1

        # 汇总
        banner("汇总")
        print(f"  成功: {passed}   失败: {failed}   跳过: {len(args.skip)}")
        print()


if __name__ == "__main__":
    main()
