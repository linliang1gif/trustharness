# -*- coding: utf-8 -*-
"""trustharness 命令行。

    trustharness inspect --cases cases.json
        对一批用例做自检，输出问题分布

    trustharness triage --results results.json
        对一批执行结果做失败分诊

    trustharness score --cases cases.json [--results results.json]
        综合评分。只给 cases 出静态分，同时给 results 出完整分

    trustharness convention --spec swagger.json [--results results.json]
        探测接口的错误表达约定

所有命令都支持 --json 输出机器可读结果，便于嵌入 CI。
score / run 另有 --junit（JUnit XML，给 CI 的测试报告页）和 --summary（Markdown 摘要）。
退出码：0 通过；1 低于 --min-score 阈值；2 输入有误。
执行有效性会给分数封顶，一次请求没打进去的执行过不了任何像样的阈值。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, List

from trustharness import inspect_cases, triage_failures
from trustharness.conventions import infer_from_spec, learn_from_history, merge
from trustharness.score import compute as compute_score
from trustharness.selfcheck import ERROR, summarize as summarize_selfcheck
from trustharness.triage import CATEGORY_LABELS

EXIT_OK = 0
EXIT_BELOW_THRESHOLD = 1
EXIT_BAD_INPUT = 2


def _load(path: str) -> Any:
    file = Path(path)
    if not file.exists():
        raise SystemExit(f"文件不存在: {path}")
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"不是合法 JSON: {path} —— {exc}")


def _as_list(data: Any, *keys: str) -> List[Any]:
    """输入可以是裸数组，也可以是 {cases: [...]} / {items: [...]} 这类信封。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in (*keys, "items", "data", "results", "cases"):
            if isinstance(data.get(key), list):
                return data[key]
    raise SystemExit("期望一个数组，或含数组字段的对象")


def _bar(percent: float, width: int = 24) -> str:
    filled = int(round(percent / 100 * width))
    return "█" * filled + "·" * (width - filled)


def _print_score(score) -> None:
    overall = "N/A" if score.overall is None else f"{score.overall:.1f}"
    print(f"信任度评分  {overall} / 100    等级 {score.grade}")
    execution = score.execution or {}
    if execution.get("validity_percent") is not None:
        # 分数被压到 0 时只给一个 0，人会以为是用例写得烂——两个数并排才看得出是谁压的
        capped = (f"   封顶前 {score.raw_overall:.1f}"
                  if score.raw_overall is not None and score.raw_overall != score.overall else "")
        print(f"请求真正到达 {execution['validity_percent']}%{capped}")
    print()
    for dim in score.dimensions:
        if dim.score is None:
            print(f"  {dim.label:12}    ——    {dim.detail}")
            continue
        print(f"  {dim.label:12} {dim.score:5.1f}  {_bar(dim.score)}  {dim.detail}")
    if score.caveats:
        print()
        print("  需要注意:")
        for caveat in score.caveats:
            print(f"    · {caveat}")

# ── inspect ─────────────────────────────────────────────
def cmd_inspect(args) -> int:
    cases = _as_list(_load(args.cases), "cases")
    results = inspect_cases(cases)
    summary = summarize_selfcheck(results)

    if args.json:
        print(json.dumps({"summary": summary,
                          "issues": {k: v for k, v in results.items() if v}},
                         ensure_ascii=False, indent=2))
        return EXIT_OK

    print(f"用例自检 —— 共 {summary['total_cases']} 条")
    print(f"  干净      {summary['clean_cases']:6d}   通过率 {summary['pass_rate'] * 100:.1f}%")
    print(f"  应拦截    {summary['blocked_cases']:6d}   （含 error 级问题）")
    if summary["by_code"]:
        print()
        print("  问题分布:")
        for item in summary["by_code"]:
            print(f"    {item['count']:6d}  {item['code']}")
    return EXIT_OK


# ── triage ──────────────────────────────────────────────
def cmd_triage(args) -> int:
    results = _as_list(_load(args.results), "results", "run_cases")
    verdicts = triage_failures(results)

    if args.json:
        print(json.dumps(verdicts, ensure_ascii=False, indent=2))
        return EXIT_OK

    if not verdicts:
        print("没有失败需要分诊。")
        return EXIT_OK

    from collections import Counter
    by_category = Counter(v["category"] for v in verdicts)
    by_reason = Counter(v["reason"][:70] for v in verdicts)
    total = len(verdicts)
    actionable = sum(1 for v in verdicts if v["actionable_as_defect"])

    print(f"失败分诊 —— 共 {total} 条失败")
    print(f"  值得提缺陷 {actionable}   噪声率 {(1 - actionable / total) * 100:.1f}%")
    print()
    for category, count in by_category.most_common():
        pct = count / total * 100
        print(f"  {count:6d} {pct:5.1f}%  {_bar(pct)}  {CATEGORY_LABELS.get(category, category)}")
    print()
    print("  主要根因:")
    for reason, count in by_reason.most_common(5):
        print(f"    {count:6d}  {reason}")
    return EXIT_OK


# ── convention ──────────────────────────────────────────
def cmd_convention(args) -> int:
    spec_conv = infer_from_spec(_load(args.spec)) if args.spec else None
    hist_conv = None
    if args.results:
        rows = _as_list(_load(args.results), "results", "run_cases")
        snapshots = [r.get("response_snapshot") or r.get("response")
                     for r in rows if isinstance(r, dict)]
        hist_conv = learn_from_history(snapshots)

    conv = merge(spec_conv, hist_conv)
    if args.json:
        payload = {"merged": conv.to_dict()}
        if spec_conv:
            payload["from_spec"] = spec_conv.to_dict()
        if hist_conv:
            payload["from_history"] = hist_conv.to_dict()
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return EXIT_OK

    print("接口错误约定")
    for label, c in (("spec 推断", spec_conv), ("历史学习", hist_conv), ("采纳", conv)):
        if not c:
            continue
        print(f"  {label:10} {c.style:16} 置信度 {c.confidence:.2f}  样本 {c.sample_size}")
        for note in c.notes:
            print(f"             {note}")
    return EXIT_OK


# ── CI 产物 ─────────────────────────────────────────────
def _write(path: str, text: str) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")


def _write_reports(args, *, score, cases, selfcheck, rows=None, verdicts=None,
                   execution=None, notes=None) -> None:
    from trustharness.ci import junit_xml, markdown_summary

    if getattr(args, "junit", None):
        _write(args.junit, junit_xml(score=score, cases=cases, selfcheck=selfcheck,
                                     rows=rows, verdicts=verdicts, execution=execution,
                                     min_score=args.min_score))
    if getattr(args, "summary", None):
        _write(args.summary, markdown_summary(score=score, execution=execution,
                                              verdicts=verdicts, notes=notes,
                                              min_score=args.min_score))


def _with_intent(rows: List[Any], cases: List[Any]) -> List[Any]:
    """执行结果里常常没有用例意图，按用例 id 从用例文件补上。

    意图决定一条结果能不能拿来判环境、失败该归谁。结果文件缺意图时不补，
    正向用例一条都认不出来，令牌过期的执行就判不出「不算数」。
    """
    by_id = {str(c.get("id") or c.get("case_id") or i): c
             for i, c in enumerate(cases) if isinstance(c, dict)}
    enriched = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        case = by_id.get(str(row.get("case_id") or row.get("test_case_id")
                             or row.get("id") or index))
        if case:
            row = {**row, **{key: case[key] for key in ("data_type", "expected_behavior", "title")
                             if row.get(key) is None and case.get(key) is not None}}
        enriched.append(row)
    return enriched


# ── score ───────────────────────────────────────────────
def cmd_score(args) -> int:
    """给一套既有用例 + 执行结果打分。

    有执行结果时走和 run 同一条路（pipeline.assess）：先判这一轮打没打进去，
    再带着这个上下文分诊、打分、封顶。以前这里直接 分诊→评分，
    令牌过期的那种执行照样拿 C+——而这正是 CI 里最常用的入口。
    """
    from trustharness.pipeline import assess, failure_rows

    cases = _as_list(_load(args.cases), "cases")
    selfcheck = inspect_cases(cases)

    rows, verdicts, execution, notes = None, None, None, []
    if args.results:
        rows = _with_intent(_as_list(_load(args.results), "results", "run_cases"), cases)
        assessed = assess(selfcheck=selfcheck, rows=rows)
        score = assessed["score"]
        verdicts, execution, notes = assessed["verdicts"], assessed["execution"], assessed["notes"]
    else:
        score = compute_score(selfcheck_results=selfcheck)

    if args.json:
        payload = score.to_dict()
        if verdicts is not None:
            payload["failures"] = failure_rows(verdicts)
            payload["notes"] = notes
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_score(score)
        # 「这次执行不算数」同时出现在 caveats 和 notes 里，只说一遍
        for note in notes:
            if note not in score.caveats:
                print(f"  · {note}")

    _write_reports(args, score=score, cases=cases, selfcheck=selfcheck, rows=rows,
                   verdicts=verdicts, execution=execution, notes=notes)

    if args.min_score is not None:
        if score.overall is None or score.overall < args.min_score:
            if not args.json:
                print()
                print(f"低于阈值 {args.min_score}，判定不通过。")
            return EXIT_BELOW_THRESHOLD
    return EXIT_OK



# ── run ─────────────────────────────────────────────────
def cmd_run(args) -> int:
    """接入 → 生成 → 自检 → 执行 → 分诊 → 评分，一条命令跑完。"""
    from trustharness.pipeline import RunnerUnavailable, load_spec, run as run_pipeline

    try:
        spec = load_spec(args.spec)
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc))

    headers = {}
    for raw in args.header or []:
        name, sep, value = raw.partition(":")
        if not sep or not name.strip():
            # 不回显原值：它多半是个令牌
            raise SystemExit("--header 格式应为 'Name: value'，例如 'Authorization: Bearer $TOKEN'")
        headers[name.strip()] = value.strip()

    try:
        outcome = run_pipeline(
            spec=spec,
            base_url=args.base_url or "",
            max_cases=args.max_cases,
            execute=not args.no_execute,
            skip_blocked=not args.no_skip_blocked,
            allow_mutations=args.allow_mutations,
            probe_first=not args.no_probe,
            db_path=args.db,
            verify_ssl=args.verify_ssl,
            timeout=args.timeout,
            headers=headers or None,
        )
    except RunnerUnavailable as exc:
        raise SystemExit(str(exc))

    if args.json:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False, indent=2))
    else:
        conv = outcome.convention or {}
        print(f"接口错误约定  {conv.get('style', '未知')}"
              f"  置信度 {conv.get('confidence', 0):.2f}  来源 {conv.get('source', '-')}")
        for note in (conv.get("notes") or [])[:2]:
            print(f"              {note}")
        print()
        print(f"生成用例      {outcome.generated}")
        print(f"自检拦截      {outcome.blocked}"
              f"   （含 error 级问题，默认不进入执行）")
        print(f"实际执行      {outcome.executed}")
        print(f"失败          {outcome.failures}")
        execution = outcome.execution or {}
        if execution.get("validity_percent") is not None:
            print(f"请求真正到达  {execution['validity_percent']}%"
                  f"   （正向用例中没被鉴权/网关挡回的比例）")
        caveats = outcome.score.caveats if outcome.score else []
        notes = [n for n in outcome.notes if n not in caveats]
        if notes:
            print()
            for note in notes:
                print(f"  · {note}")
        if outcome.score:
            print()
            _print_score(outcome.score)

    _write_reports(args, score=outcome.score, cases=outcome.cases, selfcheck=outcome.selfcheck,
                   rows=outcome.results, verdicts=outcome.verdicts,
                   execution=outcome.execution, notes=outcome.notes)

    score = outcome.score
    if args.min_score is not None:
        if not score or score.overall is None or score.overall < args.min_score:
            if not args.json:
                print()
                print(f"低于阈值 {args.min_score}，判定不通过。")
            return EXIT_BELOW_THRESHOLD
    return EXIT_OK


def cmd_spec_from_spring(args) -> int:
    import json

    from trustharness.spring_spec import SpringProject, inline_refs

    project = SpringProject(args.source, classpath=args.classpath or [])
    spec = project.to_openapi(title=args.title, server_prefix=args.prefix or "")
    if args.inline:
        spec["paths"] = inline_refs(spec["paths"], spec["components"]["schemas"])
        spec["components"] = {}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    ops = [op for item in spec["paths"].values() for op in item.values()]
    with_body = [op for op in ops if "requestBody" in op]
    untyped = [op for op in with_body
               if not op["requestBody"]["content"].get("application/json", {"schema": {"x": 1}})["schema"]]
    print(f"接口 {len(ops)} 个，带请求体 {len(with_body)}，"
          f"带权限码 {sum(1 for op in ops if op.get('x-permission'))}")
    if untyped:
        print(f"请求体类型没认出来的 {len(untyped)} 个（未写 schema，不瞎猜）：")
        for op in untyped[:10]:
            print(f"   {op['operationId']}")
    if project.unresolved_controllers:
        print(f"类级路径前缀求值失败、未导出的控制器 {len(project.unresolved_controllers)} 个："
              f"{', '.join(sorted(set(project.unresolved_controllers))[:10])}")
    if project.missing_parents:
        print(f"请求类的父类在源码里找不到 {len(project.missing_parents)} 个，这些类继承来的字段没有写进规范：")
        for parent, children in sorted(project.missing_parents.items(), key=lambda kv: -len(kv[1]))[:10]:
            print(f"   {parent}（{len(children)} 个请求类继承它）")
        print("   公共模块源码在别的目录时用 --classpath 加进来")
    print(f"已写入 {out}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trustharness",
        description="测试可信度评测 —— 回答「这套测试跑出来的结论有多可信」")
    parser.add_argument("--version", action="version", version=_version())
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("inspect", help="用例自检")
    p.add_argument("--cases", required=True, help="用例 JSON 文件")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("triage", help="失败分诊")
    p.add_argument("--results", required=True, help="执行结果 JSON 文件")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("convention", help="探测接口错误约定")
    p.add_argument("--spec", help="OpenAPI / Swagger 文件")
    p.add_argument("--results", help="执行结果 JSON（有则优先于 spec）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_convention)

    p = sub.add_parser("run", help="接入→生成→自检→执行→分诊→评分，一条命令跑完")
    p.add_argument("--spec", required=True, help="OpenAPI / Swagger 文件")
    p.add_argument("--base-url", help="被测环境地址；缺省时只做生成+自检，出静态分")
    p.add_argument("--max-cases", type=int, default=0, help="0 表示不限")
    p.add_argument("--no-execute", action="store_true", help="只生成与自检，不发请求")
    p.add_argument("--no-skip-blocked", action="store_true",
                   help="强制执行被自检拦截的用例（默认排除）")
    p.add_argument("--no-probe", action="store_true",
                   help="不在生成前探测真实响应。默认会打几个无参 GET 学接口"
                        "的错误约定与响应信封——spec 往往不足以判定")
    p.add_argument("--allow-mutations", action="store_true",
                   help="允许发送 POST/PUT/PATCH/DELETE。默认只发 GET/HEAD/OPTIONS——"
                        "本工具会被指向任意目标，默认就能删数据是不可接受的")
    p.add_argument("--header", action="append", metavar="'Name: value'",
                   help="每个请求都带上的请求头，可重复，通常是鉴权头。"
                        "auth_missing 用例不带——它们测的就是没令牌会不会被拒")
    p.add_argument("--db", default="output/trustharness_results.db", help="执行结果落库路径")
    p.add_argument("--verify-ssl", action="store_true")
    p.add_argument("--timeout", type=float, default=30.0)
    p.add_argument("--min-score", type=float)
    p.add_argument("--json", action="store_true")
    _add_report_flags(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("score", help="综合信任度评分")
    p.add_argument("--cases", required=True)
    p.add_argument("--results", help="执行结果 JSON；缺省时只出静态分")
    p.add_argument("--min-score", type=float,
                   help="低于此分退出码为 1，便于在 CI 里做门禁")
    p.add_argument("--json", action="store_true")
    _add_report_flags(p)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("spec-from-spring",
                       help="没有带参数的接口文档时，从 Spring 后端源码推导 OpenAPI")
    p.add_argument("--source", required=True, help="后端服务源码目录（只放一个服务，避免不同服务的同名路由混在一起）")
    p.add_argument("--out", required=True, help="输出的 OpenAPI JSON 路径")
    p.add_argument("--title", default="Spring 源码推导")
    p.add_argument("--classpath", action="append", metavar="DIR",
                   help="依赖的公共模块源码目录，可重复。只从中解析类，不导出其中的接口")
    p.add_argument("--prefix", help="给所有路径加的前缀，如网关上的服务名")
    p.add_argument("--inline", action="store_true",
                   help="把 $ref 展开成内联 schema，给只认内联结构的生成器用")
    p.set_defaults(func=cmd_spec_from_spring)
    return parser


def _add_report_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--junit", metavar="PATH",
                   help="写 JUnit XML（GitLab artifacts:reports:junit、Jenkins 等直接识别）")
    p.add_argument("--summary", metavar="PATH",
                   help="写 Markdown 摘要（可追加到 $GITHUB_STEP_SUMMARY 或贴进 MR）")


def _version() -> str:
    from trustharness import __version__
    return f"trustharness {__version__}"


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit as exc:
        if isinstance(exc.code, str):
            print(exc.code, file=sys.stderr)
            return EXIT_BAD_INPUT
        raise


if __name__ == "__main__":
    sys.exit(main())
