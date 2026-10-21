# -*- coding: utf-8 -*-
"""接口错误约定探测 (Error Convention Inference)。"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 错误表达风格
HTTP_STATUS = "http_status"        # 用 HTTP 4xx/5xx 表达错误
BUSINESS_CODE = "business_code"    # 恒定 HTTP 200，错误写在 body.code
MIXED = "mixed"                    # 两者都用
UNKNOWN = "unknown"                # 证据不足

# 常见的业务状态字段名，按优先级
CODE_FIELD_CANDIDATES = ("code", "errCode", "errcode", "resultCode", "status", "success")

# 常见的「业务成功」取值
SUCCESS_CODE_VALUES = {0, 200, "0", "200", "success", "SUCCESS", "ok", "OK", True}


@dataclass
class ErrorConvention:
    """一套接口如何表达错误。"""
    style: str = UNKNOWN
    code_field: Optional[str] = None
    success_codes: List[Any] = field(default_factory=list)
    error_http_codes: List[int] = field(default_factory=list)
    success_http_codes: List[int] = field(default_factory=lambda: [200])
    confidence: float = 0.0
    sample_size: int = 0
    source: str = "default"
    # 响应信封的顶层字段。空列表表示「已探测且确认没有信封」，
    # None 表示「证据不足，无法判定」——两者对生成断言的含义不同。
    envelope_fields: Optional[List[str]] = None
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def asserts_http_errors(self) -> bool:
        """异常用例是否应该断言 HTTP 错误码。"""
        return self.style in (HTTP_STATUS, MIXED)

    @property
    def asserts_business_code(self) -> bool:
        """异常用例是否应该断言业务错误码。"""
        if self.envelope_fields is not None and self.code_field not in self.envelope_fields:
            return False          # 已确认响应里没有该字段，断它必然失败
        return self.style in (BUSINESS_CODE, MIXED)

    def has_envelope_field(self, name: str) -> Optional[bool]:
        """响应信封里有没有这个顶层字段。None 表示证据不足。"""
        if self.envelope_fields is None:
            return None
        return name in self.envelope_fields


def default_convention() -> ErrorConvention:
    """证据不足时的保守默认：两种都接受，不武断。

    宁可断言宽松，也不要制造必然失败的断言——后者会淹没真实缺陷。
    """
    return ErrorConvention(
        style=MIXED,
        code_field="code",
        success_codes=[0, 200],
        error_http_codes=[400, 401, 403, 404, 422, 500],
        confidence=0.3,
        source="default",
        notes=["证据不足，采用宽松默认；接入项目后建议跑一次历史学习"],
    )


# ── 响应信封探测 ────────────────────────────────────────────
# 生成器过去无条件给每条用例加 field_exists:code，假设所有接口都返回
# {message, version, status}，根本没有 code —— 该断言在 95% 的接口上必然失败。
# 信封字段必须探测，不能假设。

ENVELOPE_VOCABULARY = frozenset({
    "code", "errCode", "errcode", "resultCode", "status", "success",
    "message", "msg", "errMsg", "data", "result", "payload",
})


def _resolve_ref(node: Any, spec: Dict[str, Any], depth: int = 0) -> Dict[str, Any]:
    """解开 $ref。FastAPI / OpenAPI 3 的响应 schema 基本都是引用。"""
    if depth > 6 or not isinstance(node, dict):
        return {}
    ref = node.get("$ref")
    if not ref:
        return node
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    cursor: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(cursor, dict):
            return {}
        cursor = cursor.get(part)
        if cursor is None:
            return {}
    return _resolve_ref(cursor, spec, depth + 1)


def _success_schema(operation: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    responses = operation.get("responses") or {}
    body = responses.get("200") or responses.get(200) or {}
    if not isinstance(body, dict):
        return {}
    schema = body.get("schema")
    if schema is None:
        content = body.get("content") or {}
        schema = (content.get("application/json") or {}).get("schema")
    return _resolve_ref(schema or {}, spec)


def detect_envelope_from_spec(spec: Dict[str, Any]) -> Optional[List[str]]:
    """从 spec 的 200 响应 schema 里统计顶层字段。

    返回 None 表示 spec 里没有可用 schema（如 YApi 导出），无法判定。
    """
    if not isinstance(spec, dict):
        return None
    counts = Counter()
    with_schema = 0
    for op in _iter_operations(spec):
        props = (_success_schema(op, spec) or {}).get("properties")
        if not isinstance(props, dict) or not props:
            continue
        with_schema += 1
        for name in props:
            counts[name] += 1
    # 覆盖率不足时不下结论。平台自身的 spec 只有 34/356 个操作带 schema，
    # 据此判定「没有 message 字段」会误删有效断言——实际响应里是有的。
    # 宁可说「不知道」并保留断言，也不要凭薄证据削弱测试。
    total_ops = sum(1 for _ in _iter_operations(spec))
    if with_schema < 3 or (total_ops and with_schema / total_ops < 0.3):
        return None
    # 出现在至少三成有 schema 的接口上，才算信封字段
    threshold = max(2, with_schema * 0.3)
    return sorted(n for n, c in counts.items()
                  if c >= threshold and n in ENVELOPE_VOCABULARY)


def detect_envelope_from_history(snapshots: Iterable[Any]) -> Optional[List[str]]:
    """从真实响应体里统计顶层字段。比 spec 可信。"""
    counts = Counter()
    total = 0
    for snap in snapshots:
        _http, body = _extract(snap)
        if not isinstance(body, dict):
            continue
        total += 1
        for name in body:
            counts[name] += 1
    if total < 10:
        return None
    threshold = max(2, total * 0.5)
    return sorted(n for n, c in counts.items()
                  if c >= threshold and n in ENVELOPE_VOCABULARY)

# ── 1. 从 OpenAPI / Swagger 推断 ────────────────────────────
def _iter_operations(spec: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    for _path, methods in (spec.get("paths") or {}).items():
        if not isinstance(methods, dict):
            continue
        for method, op in methods.items():
            if method.lower() in ("get", "post", "put", "delete", "patch") and isinstance(op, dict):
                yield op


def infer_from_spec(spec: Dict[str, Any]) -> ErrorConvention:
    """从 spec 声明的响应码推断错误约定。"""
    if not isinstance(spec, dict):
        return default_convention()

    declared = Counter()
    ops = 0
    for op in _iter_operations(spec):
        ops += 1
        for code in (op.get("responses") or {}):
            try:
                declared[int(str(code))] += 1
            except (TypeError, ValueError):
                declared[str(code)] += 1

    if ops == 0:
        return default_convention()

    error_declared = sum(n for c, n in declared.items()
                         if isinstance(c, int) and 400 <= c < 600)
    success_declared = sum(n for c, n in declared.items()
                           if isinstance(c, int) and 200 <= c < 300)

    conv = ErrorConvention(source="spec", sample_size=ops)
    conv.envelope_fields = detect_envelope_from_spec(spec)

    if error_declared == 0 and success_declared > 0:
        # 一条错误响应都没声明 —— 要么文档不全，要么接口恒返 200。
        # 两种情况下都不该断言 HTTP 4xx：前者是没依据，后者是会必然失败。
        conv.style = BUSINESS_CODE
        conv.code_field = "code"
        conv.success_codes = [0, 200]
        conv.confidence = 0.6 if ops >= 20 else 0.4
        conv.notes.append(
            f"{ops} 个接口全部只声明了 2xx，未声明任何错误响应；"
            "不对异常用例断言 HTTP 错误码"
        )
    elif error_declared > 0:
        ratio = error_declared / max(ops, 1)
        conv.style = HTTP_STATUS if ratio >= 0.5 else MIXED
        conv.error_http_codes = sorted(
            c for c in declared if isinstance(c, int) and 400 <= c < 600)
        conv.code_field = "code"
        conv.success_codes = [0, 200]
        conv.confidence = min(0.5 + ratio / 2, 0.9)
        conv.notes.append(f"{error_declared}/{ops} 个接口声明了错误响应")
    else:
        return default_convention()

    conv.success_http_codes = sorted(
        c for c in declared if isinstance(c, int) and 200 <= c < 300) or [200]
    return conv


# ── 2. 从历史执行快照学习 ───────────────────────────────────
def _extract(snapshot: Any) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
    if isinstance(snapshot, str):
        try:
            snapshot = json.loads(snapshot)
        except (ValueError, TypeError):
            return None, None
    if not isinstance(snapshot, dict):
        return None, None
    http = snapshot.get("status_code") or snapshot.get("statusCode")
    try:
        http = int(http) if http is not None else None
    except (TypeError, ValueError):
        http = None
    body = snapshot.get("body")
    if not isinstance(body, dict):
        body = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else None
    return http, body


def learn_from_history(snapshots: Iterable[Any]) -> ErrorConvention:
    """从真实响应快照里统计接口实际怎么回错误。

    实际行为比文档可信，所以本结果优先于 infer_from_spec。
    """
    http_errors = 0
    biz_errors = 0
    ok_200 = 0
    field_hits = Counter()
    success_values = Counter()
    total = 0
    snapshots_seen = list(snapshots)   # 需要二次扫描来探测信封字段

    for snap in snapshots_seen:
        http, body = _extract(snap)
        if http is None:
            continue
        total += 1

        if isinstance(body, dict):
            for name in CODE_FIELD_CANDIDATES:
                if name in body:
                    field_hits[name] += 1
                    break

        if 400 <= http < 600:
            http_errors += 1
            continue
        if http != 200 or not isinstance(body, dict):
            continue

        code_field = next((n for n in CODE_FIELD_CANDIDATES if n in body), None)
        if code_field is None:
            ok_200 += 1
            continue
        value = body[code_field]
        if value in SUCCESS_CODE_VALUES:
            ok_200 += 1
            success_values[str(value)] += 1
        else:
            biz_errors += 1

    if total < 10:
        conv = default_convention()
        conv.envelope_fields = detect_envelope_from_history(snapshots_seen)
        conv.notes.append(f"历史样本仅 {total} 条，不足以学习，沿用默认")
        return conv

    conv = ErrorConvention(source="history", sample_size=total)
    conv.envelope_fields = detect_envelope_from_history(snapshots_seen)
    conv.code_field = field_hits.most_common(1)[0][0] if field_hits else "code"
    conv.success_codes = ([json.loads(v) if v.lstrip("-").isdigit() else v
                           for v, _ in success_values.most_common(3)]
                          or [0, 200])

    total_errors = http_errors + biz_errors
    if total_errors == 0:
        conv.style = UNKNOWN
        conv.confidence = 0.2
        conv.notes.append(f"{total} 条样本里没有观察到任何错误响应，无法判定约定")
        return conv

    biz_ratio = biz_errors / total_errors
    if biz_ratio >= 0.8:
        conv.style = BUSINESS_CODE
    elif biz_ratio <= 0.2:
        conv.style = HTTP_STATUS
    else:
        conv.style = MIXED

    conv.error_http_codes = [400, 401, 403, 404, 422, 500] if http_errors else []
    conv.confidence = min(0.5 + min(total, 500) / 1000, 0.95)
    conv.notes.append(
        f"{total} 条样本：业务码错误 {biz_errors} 次、HTTP 错误 {http_errors} 次、"
        f"成功 {ok_200} 次；业务码占错误的 {biz_ratio * 100:.0f}%"
    )
    return conv


def merge(spec_conv: Optional[ErrorConvention],
          history_conv: Optional[ErrorConvention]) -> ErrorConvention:
    """合并两个信息源。实际行为优先于文档声明。"""
    candidates = [c for c in (history_conv, spec_conv) if c and c.style != UNKNOWN]
    if not candidates:
        merged = default_convention()
    else:
        merged = max(candidates, key=lambda c: c.confidence)
        if len(candidates) > 1:
            other = [c for c in candidates if c is not merged][0]
            if other.style != merged.style:
                merged.notes.append(
                    f"另一信息源（{other.source}）判定为 {other.style}，"
                    f"置信度 {other.confidence:.2f}，已按置信度取高者"
                )

    # 错误风格与响应信封是两个正交的事实，不该被同一次「二选一」绑在一起。
    # 早先版本整体取置信度高的那个，结果 spec 的 style 赢了、
    # 而历史观测到的信封字段被一并丢弃，field_exists:code 继续照发。
    # 信封以直接观测为准：历史 > spec。
    for source in (history_conv, spec_conv):
        if source is not None and source.envelope_fields is not None:
            if merged.envelope_fields != source.envelope_fields:
                merged.envelope_fields = source.envelope_fields
                merged.notes.append(
                    f"响应信封取自 {source.source}：{source.envelope_fields}")
            break
    return merged


# ── 3. 按约定生成断言 ───────────────────────────────────────
def rewrite_assertions(existing: Optional[List[Dict[str, Any]]],
                       conv: ErrorConvention, *, intent: str,
                       response_time_ms: int = 10000,
                       expected_behavior: Optional[str] = None) -> List[Dict[str, Any]]:
    """外科式重写：只替换与错误约定相关的状态断言，保留其余断言。"""
    # 这些断言的正确取值取决于接口的错误约定，需要重写；
    # 旧类型名保留在集合里，以便重写历史上生成的用例。
    convention_owned = {
        "status_code", "status_code_in", "status_code_lt", "status_code_not_in",
        "business_code", "business_code_in", "business_code_not_in",
        "error_signalled",
    }

    kept: List[Dict[str, Any]] = []
    had_response_time = False
    dropped_envelope = 0
    for a in (existing or []):
        if not isinstance(a, dict):
            continue
        kind = str(a.get("type") or a.get("assertion_type") or "")
        if kind in convention_owned:
            continue
        # 生成器无条件加了 field_exists:code / field_equals:code==200 /
        # field_exists:data.list 这类信封断言。若已确认响应没有该信封字段，
        if _targets_absent_envelope_field(a, conv):
            dropped_envelope += 1
            continue
        if kind == "response_time":
            had_response_time = True
        kept.append(a)

    fresh = build_assertions(conv, intent=intent, response_time_ms=response_time_ms,
                             expected_behavior=expected_behavior)
    if had_response_time:
        fresh = [a for a in fresh if a.get("type") != "response_time"]

    # 重写要能重复执行：上一轮写进去的业务码断言（json_path）不属于「约定类型」会被保留，
    # 这一轮又生成一条一样的，断言就越叠越多
    kept = [a for a in kept if a not in fresh]

    # 约定断言在前（先判成败），保留的结构断言在后
    return fresh + kept


def _targets_absent_envelope_field(assertion: Dict[str, Any],
                                   conv: ErrorConvention) -> bool:
    """这条断言是否指向一个「已确认不存在」的信封字段。

    只在有确凿证据（envelope_fields 非 None）时才判 True。
    证据不足时一律保留断言——宁可保守，也不要悄悄削弱测试。
    """
    if conv.envelope_fields is None:
        return False
    kind = str(assertion.get("type") or assertion.get("assertion_type") or "")
    if kind not in ("field_exists", "field_equals", "json_path"):
        return False
    path = assertion.get("path") or assertion.get("field") or ""
    if not isinstance(path, str) or not path:
        return False
    root = path.split(".")[0].split("[")[0].strip()
    if root not in ENVELOPE_VOCABULARY:
        return False          # 业务字段断言不动，只管信封字段
    return root not in conv.envelope_fields


def build_assertions(conv: ErrorConvention, *, intent: str,
                     response_time_ms: int = 10000,
                     expected_behavior: Optional[str] = None) -> List[Dict[str, Any]]:
    """按错误约定与用例意图生成断言。

    这是替换硬编码模板的入口：异常用例不再一律断言 HTTP 4xx，
    而是断言「这套接口表达错误的那种方式」。

    只产出执行引擎已支持的标准断言类型（status_code / json_path /
    response_time 等），靠 operator 表达差异。早先版本发明了
    status_code_in / business_code_not_in 这类新类型，执行引擎不认识，
    一律判失败——生成的用例在自检里 100% 通过、执行时 100% 失败。
    生成的用例应当能被任何执行器跑，不只是本包自带的那个。

    intent 可以直接传原始 data_type，这里会结合 expected_behavior 解析。
    以前是拿 intent 直接查词表：平台自愈传的是原始 data_type，
    「boundary + 声明应当成功」的用例落进「意图未知」分支，
    重写后只剩一条「状态码 < 500」，什么都拦不住。
    没有 expected_behavior 的 boundary 仍按未知处理，不猜——见 resolve_intent。
    """
    from trustharness.intent import NEGATIVE_INTENTS, POSITIVE_INTENTS, resolve_intent

    resolved = resolve_intent(intent, expected_behavior, assume_ambiguous_valid=False)
    asserts: List[Dict[str, Any]] = []
    negative = resolved in NEGATIVE_INTENTS
    positive = resolved in POSITIVE_INTENTS
    code_field = conv.code_field or "code"

    # 已确认响应里没有业务码字段时，断言它必然失败——不如不断言
    code_field_present = conv.has_envelope_field(code_field) is not False

    if negative:
        if conv.style == BUSINESS_CODE:
            # 接口恒返 200，错误写在 body —— 断言 HTTP 4xx 必然失败。
            # 鉴权例外：鉴权在业务代码之前，很多「恒返 200」的系统未登录时直接回 401/403
            http_ok = set(conv.success_http_codes)
            if resolved == "auth_missing":
                http_ok |= {401, 403}
            asserts.append({"type": "status_code", "operator": "in",
                            "expected": sorted(http_ok)})
            if code_field_present:
                asserts.append({"type": "json_path", "path": code_field, "operator": "not_in",
                                "expected": list(conv.success_codes)})
        elif conv.style == HTTP_STATUS:
            asserts.append({"type": "status_code", "operator": "in",
                            "expected": conv.error_http_codes or [400, 422]})
        else:  # MIXED：两种错误表达都接受，避免制造必然失败的断言
            accepted = sorted(set((conv.error_http_codes or [400, 422, 500])
                                  + list(conv.success_http_codes)))
            asserts.append({"type": "status_code", "operator": "in", "expected": accepted})
            if code_field_present:
                asserts.append({"type": "json_path", "path": code_field, "operator": "not_in",
                                "expected": list(conv.success_codes)})
    elif positive:
        asserts.append({"type": "status_code", "operator": "in",
                        "expected": sorted(set(conv.success_http_codes))})
        if conv.asserts_business_code and code_field_present:
            asserts.append({"type": "json_path", "path": code_field, "operator": "in",
                            "expected": list(conv.success_codes)})
    else:
        # 意图未标注：只断言「服务端没崩」，不猜业务预期
        asserts.append({"type": "status_code", "operator": "lt", "expected": 500})

    asserts.append({"type": "response_time", "expected": response_time_ms})
    return asserts
