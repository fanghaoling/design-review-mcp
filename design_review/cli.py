"""design-review CLI — 不依赖 MCP/Claude Code 也能跑审查（开源友好 + 可进 CI/脚本）。

复用 server.review_document（纯 async 函数；import server 仅触发 FastMCP 实例化 + load_dotenv，
无启动副作用）。日志走 server 配的 stderr，stdout 保持干净（json/sarif 可直接管道消费）。

用法：
  design-review plan path/to/plan.md --output markdown
  design-review plan --text "# 方案..." --dimensions planner safety
  cat plan.md | design-review plan -                     # stdin
  design-review code src/a.py src/b.py --output sarif --output-file out.sarif
  design-review doc rfc.md --type rfc
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from design_review.server import review_document


def _read_text_input(args) -> str:
    """plan/doc 输入优先级：--text > 文件路径 > stdin(-)。"""
    if args.text is not None:
        return args.text
    if args.input == "-":
        return sys.stdin.read()
    return Path(args.input).read_text(encoding="utf-8")


def _emit(result: dict, args) -> None:
    """json 输出整 dict；markdown/sarif 输出 result['rendered']。--output-file 写文件。"""
    if args.output_format == "json":
        text = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        text = result.get("rendered", "")
    if args.output_file:
        Path(args.output_file).write_text(text, encoding="utf-8")
    else:
        print(text)


def _add_review_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--adapter", default="auto", choices=["auto", "unity", "generic"])
    p.add_argument("--panel", nargs="*", default=None, help="模型列表，缺省用 config panel")
    p.add_argument("--dimensions", nargs="*", default=None)
    p.add_argument("--output", dest="output_format", default="json", choices=["json", "markdown", "sarif"])
    p.add_argument("--output-file", default=None, help="写文件（默认 stdout）")
    p.add_argument("--retrieve-top-k", type=int, default=5)
    p.add_argument("--extra-context", default="")
    p.add_argument("--effort", default=None, choices=["low", "medium", "high", "xhigh", "max"])
    p.add_argument("--max-cost-usd", type=float, default=None)
    p.add_argument("--timeout", type=float, default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="design-review", description="多模型对抗设计审查 CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="审方案/计划（markdown）")
    p_plan.add_argument("input", nargs="?", default="-", help="文件路径或 -（stdin，默认）")
    p_plan.add_argument("--text", default=None, help="直接传文本（优先于 input 文件）")
    _add_review_args(p_plan)

    p_code = sub.add_parser("code", help="审代码（传文件路径，可多个）")
    p_code.add_argument("files", nargs="+", help="代码文件路径（可多个）")
    _add_review_args(p_code)

    p_doc = sub.add_parser("doc", help="审文档（指定 --type: markdown/adr/rfc/config）")
    p_doc.add_argument("input", nargs="?", default="-", help="文件路径或 -（stdin，默认）")
    p_doc.add_argument("--text", default=None)
    p_doc.add_argument("--type", dest="document_type", default="markdown",
                       choices=["markdown", "adr", "rfc", "config"])
    _add_review_args(p_doc)

    return parser


def main() -> None:
    args = build_parser().parse_args()
    common = dict(
        adapter=args.adapter, panel=args.panel, dimensions=args.dimensions,
        output_format=args.output_format, retrieve_top_k=args.retrieve_top_k,
        extra_context=args.extra_context, effort=args.effort,
        max_cost_usd=args.max_cost_usd, timeout=args.timeout,
    )
    if args.command == "code":
        files = {f: Path(f).read_text(encoding="utf-8") for f in args.files}
        result = asyncio.run(review_document(content="", document_type="code", files=files, **common))
    else:
        content = _read_text_input(args)
        dtype = "markdown" if args.command == "plan" else args.document_type
        result = asyncio.run(review_document(content=content, document_type=dtype, **common))
    _emit(result, args)


if __name__ == "__main__":
    main()
