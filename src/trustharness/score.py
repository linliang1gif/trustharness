# -*- coding: utf-8 -*-
"""信任度评分 (Trust Score)。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

STATIC = "static"
DYNAMIC = "dynamic"

# 维度权重。静态模式下对已测维度重新归一化。
WEIGHTS = {
    "case_integrity": 0.30,
    "assertion_fitness": 0.25,
    "outcome_honesty": 0.25,
    "signal_purity": 0.20,
}

LABELS = {
    "case_integrity": "用例健全度",
    "assertion_fitness": "断言适配度",
    "outcome_honesty": "结论诚实度",
    "signal_purity": "信号纯度",
}

EXPLAIN = {
    "case_integrity": "自检未发现 error 级问题的用例占比。error 级指用例不可执行，或结果必然误判。",
    "assertion_fitness": "断言方式与接口错误约定匹配的用例占比。接口用 HTTP 200 + 业务码表达错误时，断言 HTTP 4xx 必然失配。",
    "outcome_honesty": "非「假通过」的用例占比。假通过指异常用例断言期望成功，接口正常返回于是判通过，实际什么都没验证。",
    "signal_purity": "失败中归类为「疑似真实缺陷」的占比。其余是用例侧或环境侧问题，不该占用开发的注意力。",
}

# 等级切分点。刻意把及格线放在 60，与「大部分失败是噪声」的现实对齐。
GRADE_BANDS = [
    (90, "A"), (80, "B+"), (70, "B"), (60, "C+"), (50, "C"), (35, "D"), (0, "F"),
]


@dataclass
class Dimension:
    key: str
    label: str
    score: Optional[float]          # 0-100；None 表示证据不足未打分
    weight: float
    numerator: int
    denominator: int
    detail: str
    explain: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TrustScore:
    overall: Optional[float]
    grade: str
    evidence_level: str
    dimensions: List[Dimension] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    totals: Dict[str, int] = field(default_factory=dict)
    # 执行有效性：evidence.scan() 的结果，没提供就是 None
    execution: Optional[Dict[str, Any]] = None
    # 被有效性压低之前的加权分。两个数并排放着，才看得出是被谁压的
    raw_overall: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "overall": self.overall,
            "raw_overall": self.raw_overall,
            "grade": self.grade,
            "evidence_level": self.evidence_level,
            "dimensions": [d.to_dict() for d in self.dimensions],
            "caveats": self.caveats,
            "totals": self.totals,
            "execution": self.execution,
        }


def _ratio(numerator: int, denominator: int) -> Optional[float]:
    if denominator <= 0:
        return None
    return round(numerator / denominator * 100, 1)


def _grade(score: Optional[float], evidence_level: str) -> str:
    if score is None:
        return "N/A"
    letter = next(g for threshold, g in GRADE_BANDS if score >= threshold)
    return letter if evidence_level == DYNAMIC else f"{letter}(静态)"


def compute(
    *,
    selfcheck_results: Dict[str, List[Dict[str, Any]]],
    triage_results: Optional[List[Dict[str, Any]]] = None,
    execution_evidence: Optional[Dict[str, Any]] = None,
) -> TrustScore:
    """计算信任度评分。

    selfcheck_results  —— {case_id: issues}，来自 trustharness.selfcheck
    triage_results     —— [verdict, ...]，来自 trustharness.triage；
                           为空时只出静态分
    execution_evidence —— trustharness.evidence.scan() 的结果；
                           给了就用它封顶，不给则维持旧行为
    """
    from trustharness.selfcheck import ERROR

    total_cases = len(selfcheck_results)
    caveats: List[str] = []

    # ── 用例健全度 ──
    blocked = sum(1 for issues in selfcheck_results.values()
                  if any(i["severity"] == ERROR for i in issues))
    sound = total_cases - blocked

    # ── 断言适配度 ──
    # 断言与接口错误约定失配，表现为自检里的这两类问题
    misfit_codes = {"negative_intent_expects_success", "positive_intent_expects_error",
                    "duplicate_expected", "no_assertion"}
    misfit = sum(1 for issues in selfcheck_results.values()
                 if any(i["code"] in misfit_codes for i in issues))
    fitting = total_cases - misfit

    # ── 结论诚实度 ──
    # 假通过：异常意图却断言期望成功，跑出来是绿的但什么都没验证
    false_pass = sum(1 for issues in selfcheck_results.values()
                     if any(i["code"] == "negative_intent_expects_success" for i in issues))
    honest = total_cases - false_pass

    dims = [
        Dimension("case_integrity", LABELS["case_integrity"],
                  _ratio(sound, total_cases), WEIGHTS["case_integrity"],
                  sound, total_cases,
                  f"{sound}/{total_cases} 条用例无 error 级问题",
                  EXPLAIN["case_integrity"]),
        Dimension("assertion_fitness", LABELS["assertion_fitness"],
                  _ratio(fitting, total_cases), WEIGHTS["assertion_fitness"],
                  fitting, total_cases,
                  f"{fitting}/{total_cases} 条用例的断言方式与接口约定匹配",
                  EXPLAIN["assertion_fitness"]),
        Dimension("outcome_honesty", LABELS["outcome_honesty"],
                  _ratio(honest, total_cases), WEIGHTS["outcome_honesty"],
                  honest, total_cases,
                  f"{false_pass} 条假通过用例" if false_pass else "无假通过用例",
                  EXPLAIN["outcome_honesty"]),
    ]

    # ── 信号纯度（需要执行结果）──
    # 判「有没有执行结果」必须用 is not None：空列表是「执行了，一条都没失败」，
    # 不是「没执行」。以前写的是 if triage_results，干净的执行被判成静态分、
    # 等级带「(静态)」、还提示「本次未提供执行结果」。
    evidence_level = STATIC
    if triage_results is not None:
        evidence_level = DYNAMIC
        failures = len(triage_results)
        actionable = sum(1 for v in triage_results if v.get("actionable_as_defect"))
        dims.append(Dimension(
            "signal_purity", LABELS["signal_purity"],
            _ratio(actionable, failures), WEIGHTS["signal_purity"],
            actionable, failures,
            (f"{actionable}/{failures} 条失败归类为疑似真实缺陷" if failures
             else "本次执行没有失败，没有可衡量的信号，本维度未参与评分"),
            EXPLAIN["signal_purity"]))
    else:
        dims.append(Dimension(
            "signal_purity", LABELS["signal_purity"],
            None, WEIGHTS["signal_purity"], 0, 0,
            "未提供执行结果，本维度未参与评分",
            EXPLAIN["signal_purity"]))
        caveats.append("只有静态证据：本次未提供执行结果，信号纯度未纳入评分。"
                       "跑一次真实执行后再评分，结论更完整。")

    if total_cases == 0:
        return TrustScore(None, "N/A", evidence_level, dims,
                          caveats + ["没有用例可评分"], {"cases": 0})

    # ── 加权。未打分的维度不参与，权重在已测维度间重新归一化 ──
    measured = [d for d in dims if d.score is not None]
    weight_sum = sum(d.weight for d in measured)
    overall = round(sum(d.score * d.weight for d in measured) / weight_sum, 1) if weight_sum else None

    if false_pass:
        caveats.append(
            f"{false_pass} 条用例是假通过——它们在报告里是绿的，"
            "却一个异常分支都没验证到。通过率会高估真实覆盖。")
    if blocked:
        caveats.append(f"{blocked} 条用例含 error 级问题，建议隔离后再执行。")

    # ── 执行有效性封顶 ──
    # 放在最前面插 caveat：环境不成立时，其余提醒都是次要的
    raw_overall = overall
    if execution_evidence and execution_evidence.get("validity") is not None:
        ceiling = execution_evidence["validity_percent"]
        # 只有真有问题才进 caveats。环境正常时 headline 是一句好消息，
        # 放进「需要注意」里会教人忽略这一栏——那比不提醒更糟。
        if execution_evidence.get("verdict") != "ok":
            caveats.insert(0, execution_evidence["headline"])
        if overall is not None and ceiling < overall:
            overall = ceiling
        blocked_passes = execution_evidence.get("passed_but_blocked") or 0
        if blocked_passes:
            caveats.insert(1, (
                f"{blocked_passes} 条被判「通过」的用例，请求根本没走到业务逻辑——"
                "它们不该计入通过率。"))

    return TrustScore(
        overall=overall,
        raw_overall=raw_overall,
        grade=_grade(overall, evidence_level),
        evidence_level=evidence_level,
        dimensions=dims,
        caveats=caveats,
        totals={
            "cases": total_cases,
            "sound_cases": sound,
            "blocked_cases": blocked,
            "false_pass_cases": false_pass,
            "failures": len(triage_results or []),
            "passed_but_blocked": (execution_evidence or {}).get("passed_but_blocked", 0),
        },
        execution=execution_evidence,
    )
