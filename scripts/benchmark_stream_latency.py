#!/usr/bin/env python3
"""对比「直连上游」与「经本服务 api2cursor 中转」的流式耗时。

直连、中转使用**不同 model 字段**（与你在 Cursor / curl 里配置一致），避免对比失真。

指标：
  - 首包（TTFT）：POST 发出到首条 SSE data（非 [DONE]）
  - 读流：读完整条 SSE 的时间
  - 全程墙钟：POST 到流结束

密钥说明（两把钥匙，别混用）：
  - 直连 Moonshot：Bearer = 上游 sk（-k / MOONSHOT_API_KEY）
  - 本服务 api2cursor：若服务端配置了 ACCESS_API_KEY，则 Bearer（或 x-api-key）必须等于它（-a /
    ACCESS_API_KEY）；服务再用自身的 PROXY_API_KEY 去调上游，客户端不要传 Moonshot sk 当访问钥。

最小用法：

  python scripts/benchmark_stream_latency.py -k <moonshot_sk> -a <ACCESS_API_KEY> -t hi

只测一侧、或未启用 ACCESS_API_KEY 时 -a 可省略（本服务沿用 -k）。

  python scripts/benchmark_stream_latency.py -k sk-xxx -t hi --no-proxy
  python scripts/benchmark_stream_latency.py -a only_access -t hi --no-direct
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Any

try:
    import requests
except ImportError:
    print("请先安装依赖: pip install requests", file=sys.stderr)
    sys.exit(1)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# 默认与常见 curl 对齐；可用参数或环境变量覆盖
DEFAULT_DIRECT_URL = "https://api.moonshot.cn/v1/chat/completions"
DEFAULT_PROXY_URL = "http://104.225.148.168:3029/v1/chat/completions"
#DEFAULT_DIRECT_MODEL = "kimi-k2.5"
#DEFAULT_PROXY_MODEL = "glm-kimi-k2.5"
DEFAULT_DIRECT_MODEL = "kimi-k2-0905-preview"
DEFAULT_PROXY_MODEL = "glm-kimi-k2-0905-preview"


def _collapse_duplicate_slashes(url: str) -> str:
    """把 https://host//v1/... 规范成单斜杠路径（不破坏 https://）。"""
    url = url.strip()
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "/" not in rest:
        return url
    host, path = rest.split("/", 1)
    while path.startswith("/"):
        path = path[1:]
    return f"{scheme}://{host}/{path}"


def _trunc(s: str, max_len: int) -> str:
    s = s.replace("\r\n", "\n")
    if len(s) <= max_len:
        return s
    return s[:max_len] + "…"


def _maybe_fix_utf8_mojibake(s: str) -> str:
    """把「UTF-8 字节被误当成 Latin-1 解码」的常见乱码尽量还原（如 ä¸ → 上）。"""
    if not s:
        return s
    try:
        fixed = s.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return s
    # 仅在修复后明显出现汉字时采用，避免误伤正常拉丁文
    if sum(1 for c in fixed if "\u4e00" <= c <= "\u9fff") >= 2:
        return fixed
    return s


def _consume_openai_sse(resp: requests.Response) -> dict[str, Any]:
    """读完整条 SSE，统计耗时并抽取简单可读的返回摘要。"""
    t_start = time.perf_counter()
    first_at: float | None = None
    n = 0
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    first_raw: str | None = None
    last_raw: str | None = None
    stream_error: str | None = None

    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        if first_at is None:
            first_at = time.perf_counter()
        n += 1
        last_raw = payload
        if first_raw is None:
            first_raw = payload

        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if "error" in obj and obj["error"] is not None:
            err = obj["error"]
            if isinstance(err, dict):
                em = err.get("message") or err.get("type") or json.dumps(err, ensure_ascii=False)
            else:
                em = str(err)
            stream_error = _maybe_fix_utf8_mojibake(str(em))
            break
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            rc = d.get("reasoning_content")
            if rc:
                reasoning_parts.append(str(rc))
            c = d.get("content")
            if c:
                text_parts.append(str(c))

    t_end = time.perf_counter()
    ttft = (first_at - t_start) if first_at is not None else None
    reasoning_merged = "".join(reasoning_parts)
    text_merged = "".join(text_parts)
    preview = ""
    if reasoning_merged:
        preview += f"[推理共{len(reasoning_merged)}字符] "
    preview += text_merged
    if stream_error:
        preview = f"（流式错误，非补全正文）{stream_error}"
    elif not preview.strip():
        preview = "（无 content/reasoning 增量，可能仅有 tool_calls 等分片）"

    return {
        "ttft_s": ttft,
        "stream_read_s": t_end - t_start,
        "sse_data_lines": n,
        "first_data_sample": first_raw or "",
        "last_data_sample": last_raw or "",
        "text_preview": preview,
        "stream_error": stream_error,
    }


def _one_stream(
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    timeout: float,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
    }
    wall0 = time.perf_counter()
    r = requests.post(url, headers=headers, json=body, stream=True, timeout=timeout)
    try:
        r.raise_for_status()
        stats = _consume_openai_sse(r)
    finally:
        r.close()
    out: dict[str, Any] = {
        "http_status": r.status_code,
        "wall_s": time.perf_counter() - wall0,
    }
    out.update(stats)
    return out


def _fmt(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x*1000:.1f} ms"


def main() -> int:
    env_direct = os.environ.get("MOONSHOT_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
    env_access = os.environ.get("API2CURSOR_ACCESS_KEY") or os.environ.get("ACCESS_API_KEY", "")

    p = argparse.ArgumentParser(
        description="对比直连上游与本服务中转的流式耗时；直连与本服务可用不同 model、不同鉴权密钥"
    )
    p.add_argument(
        "-k",
        "--key",
        default=env_direct,
        help="直连上游的 Bearer（Moonshot sk 等）；环境变量 MOONSHOT_API_KEY / OPENAI_API_KEY",
    )
    p.add_argument(
        "-a",
        "--access-key",
        default=env_access,
        help="访问本服务 api2cursor 的密钥（须等于服务端 ACCESS_API_KEY）；缺省则与 -k 相同",
    )
    p.add_argument(
        "-t",
        "--text",
        "--prompt",
        dest="prompt",
        default="hi",
        help="用户消息内容（默认 hi，与常见 curl 一致）",
    )
    p.add_argument(
        "--direct-url",
        default=os.environ.get("BENCHMARK_DIRECT_URL", DEFAULT_DIRECT_URL),
        help="直连 chat/completions 完整 URL（默认 Moonshot）",
    )
    p.add_argument(
        "--proxy-url",
        default=os.environ.get("BENCHMARK_PROXY_URL", DEFAULT_PROXY_URL),
        help="本服务 chat/completions 完整 URL",
    )
    p.add_argument(
        "--direct-model",
        default=os.environ.get("BENCHMARK_DIRECT_MODEL", DEFAULT_DIRECT_MODEL),
        help="直连请求 JSON 里的 model（默认 kimi-k2.5）",
    )
    p.add_argument(
        "--proxy-model",
        default=os.environ.get("BENCHMARK_PROXY_MODEL", DEFAULT_PROXY_MODEL),
        help="走本服务时 JSON 里的 model（默认 glm-kimi-k2.5，与 Cursor 映射一致）",
    )
    p.add_argument("--no-direct", action="store_true", help="跳过直连，只测本服务")
    p.add_argument("--no-proxy", action="store_true", help="跳过本服务，只测直连")
    p.add_argument("--timeout", type=float, default=120.0, help="单次请求超时（秒）")
    p.add_argument("--repeat", type=int, default=1, help="每侧重复次数（>1 打印中位数）")
    p.add_argument(
        "--sample-len",
        type=int,
        default=480,
        help="打印首/末条 data 原始 JSON 时的最大字符数（默认 480）",
    )
    p.add_argument(
        "--preview-len",
        type=int,
        default=600,
        help="打印拼接正文预览的最大字符数（默认 600）",
    )
    p.add_argument("--no-print-sample", action="store_true", help="不打印返回 data 与正文预览")
    args = p.parse_args()

    direct_key = (args.key or "").strip()
    access_raw = (args.access_key or "").strip()
    access_key = access_raw or direct_key

    if not args.no_direct and not direct_key:
        print("直连需要 -k / --key（或 MOONSHOT_API_KEY）", file=sys.stderr)
        return 2
    if not args.no_proxy and not access_key:
        print("本服务需要 -a/--access-key 或与直连共用的 -k", file=sys.stderr)
        return 2

    # 注意：不要用「本服务」in label 判断，「绕过本服务」会误判为中转鉴权。
    targets: list[tuple[str, str, str, str, bool]] = []
    if not args.no_direct:
        u = _collapse_duplicate_slashes(args.direct_url.strip())
        if u:
            targets.append(("直连上游（绕过本服务）", u, args.direct_model, direct_key, False))
    if not args.no_proxy:
        u = _collapse_duplicate_slashes(args.proxy_url.strip())
        if u:
            targets.append(("本服务 api2cursor 中转", u, args.proxy_model, access_key, True))

    if not targets:
        print("两侧都已关闭：不要同时指定 --no-direct 与 --no-proxy", file=sys.stderr)
        return 2

    print(
        f"prompt={args.prompt!r}  repeat={args.repeat}\n",
        flush=True,
    )

    for label, url, model, bearer, is_proxy in targets:
        print(f"—— {label} ——")
        print(f"  URL:   {url}")
        print(f"  model: {model}")
        if is_proxy:
            src = "ACCESS_API_KEY（-a）" if access_raw else "与 -k 相同（若 401 请单独设 -a）"
            print(f"  鉴权:  {src}\n")
        else:
            print(f"  鉴权:  上游 API Key（-k）\n")

        ttfts: list[float] = []
        reads: list[float] = []
        walls: list[float] = []
        lines_counts: list[int] = []

        for i in range(args.repeat):
            try:
                m = _one_stream(url, bearer, model, args.prompt, args.timeout)
            except requests.RequestException as e:
                print(f"  第 {i+1} 轮失败: {e}", file=sys.stderr)
                if e.response is not None:
                    try:
                        print(f"  响应体: {e.response.text[:500]}", file=sys.stderr)
                    except OSError:
                        pass
                    if e.response.status_code == 401 and is_proxy:
                        print(
                            "  提示: 本服务在 app.py 中校验的是 ACCESS_API_KEY，"
                            "与 Moonshot 的 sk 不同；请传 -a <服务端 .env 里的 ACCESS_API_KEY>。",
                            file=sys.stderr,
                        )
                continue

            if m["ttft_s"] is not None:
                ttfts.append(m["ttft_s"])
            reads.append(m["stream_read_s"])
            walls.append(m["wall_s"])
            lines_counts.append(m["sse_data_lines"])

            print(
                f"  [{i+1}/{args.repeat}] HTTP {m['http_status']}  "
                f"首包 {_fmt(m['ttft_s'])}  "
                f"读流 {_fmt(m['stream_read_s'])}  "
                f"全程 {_fmt(m['wall_s'])}  "
                f"SSE行≈{m['sse_data_lines']}",
                flush=True,
            )

            if m.get("stream_error"):
                print(
                    "  注意: HTTP 200 但 SSE 里是 error 对象（常见于上游 4xx/5xx 被包进流）；"
                    "下列耗时不能当正常补全对比。",
                    file=sys.stderr,
                    flush=True,
                )
                print(f"  错误信息: {_trunc(m['stream_error'], 1200)}", flush=True)

            if not args.no_print_sample:
                sl = args.sample_len
                pl = args.preview_len
                fd = m.get("first_data_sample") or ""
                ld = m.get("last_data_sample") or ""
                if m.get("stream_error") and fd:
                    fd = _maybe_fix_utf8_mojibake(fd)
                print(f"  首条 data（截）: {_trunc(fd, sl)}", flush=True)
                if m.get("sse_data_lines", 0) > 1 and ld and ld != fd:
                    print(f"  末条 data（截）: {_trunc(ld, sl)}", flush=True)
                print(f"  正文预览（截）: {_trunc(m.get('text_preview', ''), pl)}", flush=True)

        if args.repeat > 1 and walls:

            def med(xs: list[float]) -> float:
                return statistics.median(xs)

            print(
                f"  —— 中位数 ——  首包 {_fmt(med(ttfts) if ttfts else None)}  "
                f"读流 {_fmt(med(reads))}  全程 {_fmt(med(walls))}  "
                f"SSE行≈{int(med(lines_counts)) if lines_counts else 0}"
            )
        print(flush=True)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        raise SystemExit(130)
