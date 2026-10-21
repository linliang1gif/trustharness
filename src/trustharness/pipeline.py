# -*- coding: utf-8 -*-
"""一条命令跑完：接入 → 生成 → 自检 → 执行 → 分诊 → 评分。

把散落的四件事串成一次可复现的评测。核心价值在于**自检发生在执行之前**：
含 error 级问题的用例不会进入执行队列——它们要么根本跑不起来，
要么结果必然误判，放进去只会污染信号。

需要执行能力，因此依赖可选 extra：

    pip install "trustharness[run]"

只想评估既有用例和结果、不需要真实发请求时，用 trustharness.score，
核心包零依赖。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from trustharness import inspect_cases, triage_failures
from trustharness.conventions import (
    ErrorConvention,
    infer_from_spec,
    learn_from_history,
    merge,
    rewrite_assertions,
)
from trustharness.score import TrustScore, compute as compute_score
from trustharness.selfcheck import ERROR


# 默认只执行读操作。harness 会被指向任意目标环境，
# 而内置的 EnvGuard 只拦生产环境（靠 base_url 里的 prod/生产 判断）——
# 对 staging、本地、或任何没带这些字样的地址，DELETE 会照常发出去。
# 所以这里再加一层：破坏性方法必须显式开启。
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class RunnerUnavailable(RuntimeError):
    """执行能力未安装。"""


def _require_runner():
    try:
        from trustharness.runner.execution_engine import ExecutionEngineV2  # noqa: F401
        from trustharness.runner.swagger_to_cases import generate_cases_from_swagger_data  # noqa: F401
    except ImportError as exc:
        raise RunnerUnavailable(
            "执行能力需要额外依赖，请安装：pip install \"trustharness[run]\"\n"
            f"（缺失：{exc}）"
        ) from exc


@dataclass
class PipelineResult:
    """一次完整评测的产物。"""
    convention: Optional[Dict[str, Any]] = None
    generated: int = 0
    inspected: int = 0
    blocked: int = 0
    executed: int = 0
    failures: int = 0
    score: Optional[TrustScore] = None
    cases: List[Dict[str, Any]] = field(default_factory=list)
    results: List[Dict[str, Any]] = field(default_factory=list)
    selfcheck: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    verdicts: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # evidence.scan 的结果：这一轮请求到底有没有走到业务逻辑
    execution: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "convention": self.convention,
            "counts": {
                "generated": self.generated,
                "inspected": self.inspected,
                "blocked": self.blocked,
                "executed": self.executed,
                "failures": self.failures,
            },
            "score": self.score.to_dict() if self.score else None,
            "execution": self.execution,
            # 逐条失败的判定依据。以前 --json 只给分数不给证据：说「5 条疑似
            # 真实缺陷、A 级」却不告诉你是哪 5 条——这违背本包自己的原则
            # 「每个维度都给出分子分母，任何人都能自己复算」。
            "failures": failure_rows(self.verdicts),
            "notes": self.notes,
        }


def failure_rows(verdicts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """分诊结论里给机器读的那几列。run 和 score 的 --json 共用。"""
    return [
        {
            "case_id": v.get("case_id"),
            "category": v.get("category"),
            "owner": v.get("owner"),
            "actionable_as_defect": v.get("actionable_as_defect"),
            "confidence": v.get("confidence"),
            "reason": v.get("reason"),
            "evidence": v.get("evidence"),
        }
        for v in verdicts or []
    ]


def generate(spec: Dict[str, Any], *, max_cases: int = 0,
             generate_l2: bool = True) -> tuple:
    """从 OpenAPI/Swagger 生成用例，断言按接口错误约定生成。"""
    _require_runner()
    from trustharness.runner.swagger_to_cases import generate_cases_from_swagger_data
    return generate_cases_from_swagger_data(
        spec, max_cases=max_cases, generate_l2=generate_l2)


def probe(spec: Dict[str, Any], base_url: str, *, limit: int = 16,
          timeout: float = 10.0, verify_ssl: bool = False,
          headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """生成之前先探几个真实响应，用来学接口的错误约定与响应信封。

    只打无路径参数的 GET，最多 limit 个，纯读操作。
    默认 16 个：信封与约定的学习函数都要求至少 10 条有效样本，
    而部分端点会返回非 JSON 或需要鉴权，得留出余量。

    为什么需要：spec 可能缺少响应 schema；仅凭文档判定时容易误判
    响应信封或错误约定，进而生成不适用的断言。
    真实响应是最直接的证据。
    """
    _require_runner()
    import httpx

    paths = []
    for path, methods in (spec.get("paths") or {}).items():
        if "{" in path or not isinstance(methods, dict):
            continue
        if "get" in {m.lower() for m in methods}:
            paths.append(path)
        if len(paths) >= limit:
            break

    snapshots: List[Dict[str, Any]] = []
    root = base_url.rstrip("/")
    for path in paths:
        try:
            response = httpx.get(f"{root}/{path.lstrip('/')}", headers=headers or {},
                                 timeout=timeout, verify=verify_ssl)
        except Exception:
            continue              # 探测失败不应影响主流程
        try:
            body = response.json()
        except ValueError:
            body = None
        snapshots.append({"status_code": response.status_code, "body": body})
    return snapshots


def headers_for(case: Dict[str, Any],
                headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """一条用例实际要带的请求头：公共请求头打底，用例自己的覆盖。

    应当被鉴权拒绝的用例（auth_missing）不带公共请求头——那里面通常就是令牌，
    带上之后它们测的东西就没了，而且会「通过」得理直气壮。
    """
    from trustharness.intent import AUTH_REJECTION_INTENTS, resolve_intent

    own = dict(case.get("headers") or {})
    if not headers:
        return own
    if resolve_intent(case.get("data_type"), case.get("expected_behavior")) in AUTH_REJECTION_INTENTS:
        return own
    return {**headers, **own}


def _to_runner_case(case: Dict[str, Any], base_url: str):
    """把生成器产出的扁平用例转成执行引擎的 TestCaseV2。"""
    from trustharness.runner.models import AssertionDef, TestCaseV2

    assertions = []
    for a in case.get("assertions") or []:
        if not isinstance(a, dict):
            continue
        try:
            assertions.append(AssertionDef(
                type=a.get("type"),
                expected=a.get("expected"),
                path=a.get("path") or a.get("field") or "",
                operator=a.get("operator") or "eq",
            ))
        except TypeError:
            continue

    return TestCaseV2(
        id=str(case.get("id") or case.get("case_id") or case.get("title")),
        title=case.get("title") or "",
        method=case.get("method") or "GET",
        path=case.get("path") or case.get("url") or "/",
        base_url=base_url,
        headers=case.get("headers") or {},
        # 生成器用 params，执行引擎的字段名是 query_params
        query_params=case.get("params") or case.get("query_params") or {},
        body=case.get("body"),
        assertions=assertions,
    )


def run(
    *,
    spec: Dict[str, Any],
    base_url: str = "",
    max_cases: int = 0,
    execute: bool = True,
    skip_blocked: bool = True,
    allow_mutations: bool = False,
    probe_first: bool = True,
    db_path: str = "output/trustharness_results.db",
    verify_ssl: bool = False,
    timeout: float = 30.0,
    headers: Optional[Dict[str, str]] = None,
) -> PipelineResult:
    """完整流水线。

    execute=False 时只做生成 + 自检，产出静态分——在没有可用环境、
    或只想先看用例质量时用这个模式。

    headers 会带在探测请求和每条用例上（通常是鉴权头）。唯一例外是
    「应当被鉴权拒绝」的用例（auth_missing）：给它们也带上令牌，它们测的
    东西就没了。没有这个参数时，真实环境基本全是 401，执行有效性会如实
    判「不算数」——那是对的，但这条命令也就没法在 CI 里用。

    skip_blocked=True（默认）时，含 error 级问题的用例不进入执行。
    这是本流水线与「先跑再说」的关键区别：不可执行或必然误判的用例
    不该占用执行窗口，也不该污染失败信号。

    allow_mutations=False（默认）时只发 GET/HEAD/OPTIONS。
    对一个会被指向任意目标的工具来说，默认就能删数据是不可接受的。
    """
    result = PipelineResult()

    # ① 探测接口错误约定：spec 声明 + 真实响应，后者优先
    spec_conv: ErrorConvention = infer_from_spec(spec)
    conv = spec_conv
    if base_url and probe_first:
        snapshots = probe(spec, base_url, timeout=timeout, verify_ssl=verify_ssl,
                          headers=headers)
        if snapshots:
            conv = merge(spec_conv, learn_from_history(snapshots))
            result.notes.append(
                f"探测了 {len(snapshots)} 个真实响应；"
                f"采纳约定 {conv.style}（来源 {conv.source}）"
                + (f"，响应信封 {conv.envelope_fields}"
                   if conv.envelope_fields is not None else "")
            )
    result.convention = conv.to_dict()

    # ② 生成用例，并按探测到的约定重写断言
    cases, meta = generate(spec, max_cases=max_cases)
    for case in cases:
        case.setdefault("data_type", "valid")
        case["assertions"] = rewrite_assertions(
            case.get("assertions"), conv, intent=case.get("data_type") or "valid",
            expected_behavior=case.get("expected_behavior"))
    result.cases = cases
    result.generated = len(cases)

    # ③ 自检 —— 发生在执行之前
    result.selfcheck = inspect_cases(cases)
    result.inspected = len(result.selfcheck)
    blocked_ids = {cid for cid, issues in result.selfcheck.items()
                   if any(i["severity"] == ERROR for i in issues)}
    result.blocked = len(blocked_ids)

    if not execute:
        result.notes.append("未执行：只产出静态分（生成 + 自检）")
        result.score = compute_score(selfcheck_results=result.selfcheck)
        return result

    if not base_url:
        result.notes.append("未提供 base_url，跳过执行，只产出静态分")
        result.score = compute_score(selfcheck_results=result.selfcheck)
        return result

    # ④ 执行（默认跳过被拦截的用例）
    _require_runner()
    from trustharness.runner.execution_engine import ExecutionEngineV2

    runnable = []
    skipped_mutating = 0
    for index, case in enumerate(cases):
        case_id = str(case.get("id") or case.get("case_id") or index)
        if skip_blocked and case_id in blocked_ids:
            continue
        method = str(case.get("method") or "GET").upper()
        if not allow_mutations and method not in SAFE_METHODS:
            skipped_mutating += 1
            continue
        runnable.append(_to_runner_case(
            {**case, "id": case_id, "headers": headers_for(case, headers)}, base_url))

    if headers:
        # 只报名字不报值：这行会进 CI 日志
        result.notes.append(
            f"请求带上了 {', '.join(sorted(headers))}；auth_missing 用例不带")

    if skipped_mutating:
        result.notes.append(
            f"{skipped_mutating} 条用例涉及 {'/'.join(sorted(MUTATING_METHODS))} "
            "等写操作，默认不执行（--allow-mutations 显式开启）")

    if skip_blocked and result.blocked:
        result.notes.append(
            f"{result.blocked} 条用例含 error 级问题，已排除在执行之外"
            "（--no-skip-blocked 可强制全部执行）")

    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    engine = ExecutionEngineV2(base_url=base_url, db_path=db_path,
                               verify_ssl=verify_ssl, default_timeout=timeout)

    executions = []
    for case in runnable:
        try:
            executions.append(engine.execute_case(case))
        except Exception as exc:      # 单条失败不应中断整批
            result.notes.append(f"用例 {case.id} 执行异常: {type(exc).__name__}: {exc}")
    result.executed = len(executions)

    # ⑤ 分诊 + ⑥ 评分
    intents = {}
    for index, case in enumerate(cases):
        intents[str(case.get("id") or case.get("case_id") or index)] = (
            case.get("data_type"), case.get("expected_behavior"))

    rows = []
    for ex in executions:
        response = getattr(ex, "response", None)
        data_type, behavior = intents.get(ex.case_id, (None, None))
        rows.append({
            "case_id": ex.case_id,
            "status": ex.status,
            "error_message": getattr(ex, "error_message", None) or None,
            "error_type": None,
            "response_snapshot": response.to_dict() if response else None,
            "assertion_details": [a.to_dict() for a in (ex.assertions or [])],
            "data_type": data_type,
            "expected_behavior": behavior,
        })
    result.results = rows

    assessed = assess(selfcheck=result.selfcheck, rows=rows)
    result.verdicts = assessed["verdicts"]
    result.failures = len(result.verdicts)
    result.execution = assessed["execution"]
    result.score = assessed["score"]
    result.notes.extend(assessed["notes"])
    return result


# 执行条数少于这个数时，分数的统计意义要打折扣。
# 不改分数本身——分数是可复算的比值，改它会破坏「可复现」原则；
# 只是把「样本太小」说出来。
SMALL_SAMPLE = 30


def assess(*, selfcheck: Dict[str, List[Dict[str, Any]]],
           rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """执行结果 → 整轮判定 → 分诊 → 评分。纯函数，不发请求。

    抽出来有两个理由：
      1. 这是评测里最容易做错、也最需要测试的一段，而原来它埋在要发
         真请求的 run() 里，没法离线验证「令牌过期的一轮会不会被判死」
      2. 顺序本身就是要点——必须**先**判整轮有没有打进去，再分诊、再打分。
         分诊需要知道环境是否可用，打分需要被有效性封顶。

    rows 每条：case_id / status / response_snapshot / assertion_details /
              data_type / expected_behavior / error_message
    """
    from trustharness.evidence import UNUSABLE, environment_usable, scan_results

    execution = scan_results(rows)
    verdicts = triage_failures(rows, environment_usable=environment_usable(execution))
    score = compute_score(selfcheck_results=selfcheck, triage_results=verdicts,
                          execution_evidence=execution)

    notes = []
    if execution and execution.get("verdict") == UNUSABLE:
        notes.append(execution["headline"])
    if 0 < len(rows) < SMALL_SAMPLE:
        notes.append(
            f"只执行了 {len(rows)} 条，样本量小于 {SMALL_SAMPLE}——"
            "各维度的比值统计意义有限，别把等级当成定论")

    return {"execution": execution, "verdicts": verdicts,
            "score": score, "notes": notes}


def load_spec(path: str) -> Dict[str, Any]:
    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"spec 文件不存在: {path}")
    return json.loads(file.read_text(encoding="utf-8"))
