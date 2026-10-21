"""
Swagger → V2 TestCase 自动生成器

从 Swagger/OpenAPI 文件读取 API 定义，自动生成 Executor V2 格式的测试用例。
支持的模式：
  - /page    → 分页查询（POST + pageNum/pageSize）
  - /list    → 列表查询（POST + 空body或筛选）
  - /detail  → 详情查询（POST + uuid/id）
  - /save    → 新增（POST + 必填字段骨架）
  - /delete  → 删除（POST + uuid）
  - GET      → 直接请求
"""
import json
import logging
import re
import httpx
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── 路径模式识别 ──────────────────────────────────────
_PATTERN_MAP = [
    ("page",   re.compile(r"/page$")),
    ("list",   re.compile(r"/list$")),
    ("detail", re.compile(r"/(detail|get|info|query)$", re.I)),
    ("save",   re.compile(r"/(save|add|create|insert)$", re.I)),
    ("update", re.compile(r"/(update|edit|modify)$", re.I)),
    ("delete", re.compile(r"/(delete|remove)$", re.I)),
    ("export", re.compile(r"/(export|download)")),
    ("import", re.compile(r"/(import|upload)")),
]


def _detect_pattern(path: str) -> str:
    for name, regex in _PATTERN_MAP:
        if regex.search(path):
            return name
    return "other"


# ── $ref 解析 ────────────────────────────────────────
def _resolve_ref(ref: str, root: dict) -> dict:
    """解析 $ref 引用 (如 #/definitions/SalesOrderPageReq)"""
    if not isinstance(ref, str) or not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = root
    for p in parts:
        if not isinstance(node, dict):
            return {}
        node = node.get(p, {})
        if not node:
            return {}
    return node


def _resolve_schema(schema: dict, root: dict, depth: int = 0) -> dict:
    """递归解析 schema 中的 $ref（防止无限递归，最多5层）"""
    if not isinstance(schema, dict) or depth > 5:
        return {}
    if "$ref" in schema:
        resolved = _resolve_ref(schema["$ref"], root)
        return _resolve_schema(resolved, root, depth + 1)
    # 处理 properties 中的 $ref
    properties = schema.get("properties")
    if isinstance(properties, dict):
        for key, prop in list(properties.items()):
            if not isinstance(prop, dict):
                properties.pop(key)
                continue
            if "$ref" in prop:
                properties[key] = _resolve_schema(prop, root, depth + 1)
            elif prop.get("type") == "array" and "items" in prop:
                if isinstance(prop["items"], dict) and "$ref" in prop["items"]:
                    prop["items"] = _resolve_schema(prop["items"], root, depth + 1)
    # 处理 allOf
    if "allOf" in schema:
        merged = {}
        for sub in schema["allOf"] if isinstance(schema["allOf"], list) else []:
            resolved_sub = _resolve_schema(sub, root, depth + 1)
            merged.update(resolved_sub.get("properties", {}))
        current = schema.get("properties", {})
        schema["properties"] = {**(current if isinstance(current, dict) else {}), **merged}
    return schema


# ── 从 schema 提取属性 ────────────────────────────────
def _extract_properties(schema: dict, root: dict = None) -> Dict[str, dict]:
    """提取 JSON Schema 的 properties（支持 $ref 解析）"""
    if not isinstance(schema, dict):
        return {}
    if root:
        schema = _resolve_schema(schema, root)
    raw_props = schema.get("properties", {})
    props = {key: value for key, value in raw_props.items() if isinstance(value, dict)} \
        if isinstance(raw_props, dict) else {}
    # 处理 allOf
    all_of = schema.get("allOf", [])
    for sub in all_of if isinstance(all_of, list) else []:
        if root:
            sub = _resolve_schema(sub, root)
        if isinstance(sub, dict):
            sub_props = sub.get("properties", {})
            if isinstance(sub_props, dict):
                props.update({key: value for key, value in sub_props.items() if isinstance(value, dict)})
    return props


def _make_sample_value(prop: dict) -> Any:
    """根据 schema property 生成样本值"""
    t = prop.get("type", "string")
    desc = prop.get("description", "")
    enum = prop.get("enum")
    if enum:
        return enum[0]
    if t == "integer":
        return 1
    elif t == "number":
        return 1.0
    elif t == "boolean":
        return True
    elif t == "array":
        return []
    elif t == "object":
        return {}
    else:
        # string
        if "uuid" in desc.lower() or "uuid" in prop.get("format", ""):
            return "00000000-0000-0000-0000-000000000000"
        if "date" in desc.lower() or "date" in prop.get("format", ""):
            return "2025-01-01"
        if "id" in desc.lower():
            return "1"
        return "test"


# ── 生成单个用例 ──────────────────────────────────────
def _build_case(
    path: str,
    method: str,
    summary: str,
    tags: List[str],
    pattern: str,
    req_schema: dict,
    responses: dict,
    root_spec: dict = None,
) -> Dict[str, Any]:
    """根据 API 定义生成一个 V2 测试用例"""

    tag = tags[0] if tags else "未分类"
    title = f"[{tag}] {summary or path}" if summary else f"[{tag}] {method} {path}"

    # L1: HTTP 状态断言 + 响应时间
    base_assertions = [
        {"type": "status_code", "expected": 200},
        {"type": "response_time", "expected": 10000},
    ]
    # L2: 响应结构断言 (业务接口固定有 code + message)
    l2_assertions = [
        {"type": "field_exists", "path": "code"},
        {"type": "field_exists", "path": "message"},
    ]
    l3_assertions = [
        {"type": "field_equals", "path": "code", "expected": 200},
    ]
    # L4: 数据结构断言 (按接口模式动态生成)
    l4_assertions: List[dict] = []

    case: Dict[str, Any] = {
        "title": title,
        "method": method,
        "path": path,
        "assertions": [],
    }

    props = _extract_properties(req_schema, root_spec)

    if pattern == "page":
        case["body"] = {"pageNum": 1, "pageSize": 10}
        l4_assertions = [
            {"type": "field_exists", "path": "data"},
            {"type": "field_exists", "path": "data.list"},
            {"type": "field_exists", "path": "data.total"},
        ]
        case["assertions"] = base_assertions + l2_assertions + l3_assertions + l4_assertions

    elif pattern == "list":
        case["body"] = {}
        l4_assertions = [
            {"type": "field_exists", "path": "data"},
        ]
        case["assertions"] = base_assertions + l2_assertions + l3_assertions + l4_assertions

    elif pattern == "detail":
        body = {}
        if "uuid" in props:
            body["uuid"] = "00000000-0000-0000-0000-000000000000"
        elif "id" in props:
            body["id"] = "1"
        case["body"] = body
        # detail 用假ID可能 code!=200, 只验证连通性(HTTP 200 + 有code字段)
        case["assertions"] = base_assertions + l2_assertions

    elif pattern == "save":
        # 生成必填字段骨架（只读不写，安全）
        body = {}
        required = req_schema.get("required", [])
        for name, prop in props.items():
            if name in ("_sign", "_timestamp", "sequence", "columns"):
                continue
            if name in required or len(required) == 0:
                body[name] = _make_sample_value(prop)
        case["title"] = f"[{tag}] {summary or path} (连通性验证)"
        case["body"] = body
        # save 接口只验证连通性：HTTP 200 + 有code字段即可
        case["assertions"] = [
            {"type": "status_code", "expected": 200},
            {"type": "response_time", "expected": 10000},
            {"type": "field_exists", "path": "code"},
            {"type": "field_exists", "path": "message"},
        ]

    elif pattern == "update":
        body = {}
        for name, prop in props.items():
            if name in ("_sign", "_timestamp", "sequence", "columns"):
                continue
            body[name] = _make_sample_value(prop)
        case["title"] = f"[{tag}] {summary or path} (连通性验证)"
        case["body"] = body
        case["assertions"] = [
            {"type": "status_code", "expected": 200},
            {"type": "response_time", "expected": 10000},
            {"type": "field_exists", "path": "code"},
            {"type": "field_exists", "path": "message"},
        ]

    elif pattern == "delete":
        case["body"] = {"uuid": "00000000-0000-0000-0000-000000000000"}
        case["title"] = f"[{tag}] {summary or path} (连通性验证)"
        case["assertions"] = [
            {"type": "status_code", "expected": 200},
            {"type": "response_time", "expected": 10000},
            {"type": "field_exists", "path": "code"},
            {"type": "field_exists", "path": "message"},
        ]

    else:
        # GET or other
        if method == "GET":
            case["assertions"] = base_assertions + l2_assertions
        else:
            case["body"] = {}
            case["assertions"] = base_assertions + l2_assertions

    if method == "POST" and "headers" not in case:
        case["headers"] = {"Content-Type": "application/json"}

    return case


# ── L2: 参数变异用例生成 ─────────────────────────────
def _build_mutation_cases(
    path: str,
    method: str,
    summary: str,
    tags: List[str],
    pattern: str,
    req_schema: dict,
    root_spec: dict = None,
) -> List[Dict[str, Any]]:
    """
    L2 规则增强：基于 schema 自动生成参数变异用例。
    不依赖 AI，纯规则 + 模板。
    """
    tag = tags[0] if tags else "未分类"
    label = summary or path
    mutations: List[Dict[str, Any]] = []

    props = _extract_properties(req_schema, root_spec)
    required = req_schema.get("required", []) if req_schema else []

    # 只对有 body 的方法生成变异 (POST/PUT/PATCH)
    if method not in ("POST", "PUT", "PATCH"):
        # GET/DELETE: 只生成鉴权缺失
        mutations.append({
            "title": f"[{tag}] {label} - 鉴权缺失",
            "method": method,
            "path": path,
            "headers": {},
            "body": None,
            "data_type": "auth_missing",
            "expected_status": 401,
            "assertions": [
                {"type": "status_code_in", "expected": [401, 403]},
            ],
        })
        return mutations

    # 构建正向 body 作为基准
    base_body = {}
    for name, prop in props.items():
        if name in ("_sign", "_timestamp", "sequence", "columns"):
            continue
        base_body[name] = _make_sample_value(prop)

    # ---- 1. 必填字段缺失 ----
    for field in required:
        if field in ("_sign", "_timestamp", "sequence", "columns"):
            continue
        if field in base_body:
            body = {k: v for k, v in base_body.items() if k != field}
            mutations.append({
                "title": f"[{tag}] {label} - 必填缺失: {field}",
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "data_type": "required_missing",
                "expected_status": 400,
                "assertions": [
                    {"type": "status_code_in", "expected": [400, 422, 500]},
                    {"type": "response_time", "expected": 10000},
                ],
            })

    # ---- 2. 空值 ----
    for field in required[:3]:
        if field in base_body:
            body = {**base_body, field: ""}
            mutations.append({
                "title": f"[{tag}] {label} - 空值: {field}",
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "data_type": "empty_value",
                "expected_status": 400,
                "assertions": [
                    {"type": "status_code_in", "expected": [400, 422, 500]},
                ],
            })

    # ---- 3. 类型错误 ----
    for name, prop in list(props.items())[:5]:
        t = prop.get("type", "string")
        if t in ("integer", "number"):
            body = {**base_body, name: "not_a_number"}
            mutations.append({
                "title": f"[{tag}] {label} - 类型错误: {name}='not_a_number'",
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "data_type": "type_error",
                "expected_status": 400,
                "assertions": [
                    {"type": "status_code_in", "expected": [400, 422, 500]},
                ],
            })
        elif t == "boolean":
            body = {**base_body, name: "not_bool"}
            mutations.append({
                "title": f"[{tag}] {label} - 类型错误: {name}='not_bool'",
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "data_type": "type_error",
                "expected_status": 400,
                "assertions": [
                    {"type": "status_code_in", "expected": [400, 422, 500]},
                ],
            })

    # ---- 4. 超长字符串 ----
    # 只对声明了 maxLength 的字段造：没有长度约束时接口收下超长串是对的，
    long_fields = [(n, p) for n, p in props.items()
                   if p.get("type", "string") == "string" and p.get("maxLength") is not None]
    for name, prop in long_fields[:3]:
        if True:
            body = {**base_body, name: "A" * max(10000, int(prop["maxLength"]) + 1)}
            mutations.append({
                "title": f"[{tag}] {label} - 超长字符串: {name}",
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "data_type": "overflow",
                "expected_status": 400,
                "assertions": [
                    {"type": "status_code_in", "expected": [400, 413, 422, 500]},
                ],
            })

    # ---- 5. 非法枚举 ----
    for name, prop in props.items():
        if prop.get("enum"):
            body = {**base_body, name: "__INVALID_ENUM__"}
            mutations.append({
                "title": f"[{tag}] {label} - 非法枚举: {name}",
                "method": method,
                "path": path,
                "headers": {"Content-Type": "application/json"},
                "body": body,
                "data_type": "invalid_enum",
                "expected_status": 400,
                "assertions": [
                    {"type": "status_code_in", "expected": [400, 422, 500]},
                ],
            })

    # ---- 6. 边界值 ----
    for name, prop in list(props.items())[:3]:
        t = prop.get("type", "string")
        if t in ("integer", "number"):
            for bv, bv_label in [(-1, "-1"), (0, "0"), (2147483647, "MAX_INT")]:
                body = {**base_body, name: bv}
                # 规范声明了取值范围时，越界就该被拒、界内就该成功；
                # 没声明才是真不知道——那时不替接口下结论
                lo, hi = prop.get("minimum"), prop.get("maximum")
                behavior = None
                if lo is not None or hi is not None:
                    out_of_range = (lo is not None and bv < lo) or (hi is not None and bv > hi)
                    behavior = "client_error" if out_of_range else "success"
                mutations.append({
                    "expected_behavior": behavior,
                    "title": f"[{tag}] {label} - 边界值: {name}={bv_label}",
                    "method": method,
                    "path": path,
                    "headers": {"Content-Type": "application/json"},
                    "body": body,
                    "data_type": "boundary",
                    "expected_status": None,
                    "assertions": [
                        {"type": "status_code_in", "expected": [200, 400, 422, 500]},
                        {"type": "response_time", "expected": 10000},
                    ],
                })

    # ---- 7. 鉴权缺失 ----
    mutations.append({
        "title": f"[{tag}] {label} - 鉴权缺失",
        "method": method,
        "path": path,
        "headers": {},
        "body": base_body if base_body else {},
        "data_type": "auth_missing",
        "expected_status": 401,
        "assertions": [
            {"type": "status_code_in", "expected": [401, 403]},
        ],
    })

    # ---- 8. 不存在 ID (detail/update/delete) ----
    if pattern in ("detail", "update", "delete"):
        body = {**base_body}
        for id_field in ("uuid", "id"):
            if id_field in body:
                body[id_field] = "NONEXISTENT_99999999"
                break
        mutations.append({
            "title": f"[{tag}] {label} - 不存在ID",
            "method": method,
            "path": path,
            "headers": {"Content-Type": "application/json"},
            "body": body,
            "data_type": "nonexistent_id",
            "expected_status": 404,
            "assertions": [
                {"type": "status_code_in", "expected": [200, 400, 404, 500]},
                {"type": "response_time", "expected": 10000},
            ],
        })

    return mutations


# ── 从 Swagger 2.0 parameters 提取 body schema ───────
def _extract_body_schema_v2(parameters: list, root: dict) -> dict:
    """从 Swagger 2.0 的 parameters 列表中提取 in:body 的 schema"""
    for param in parameters:
        if param.get("in") == "body":
            schema = param.get("schema", {})
            if "$ref" in schema:
                schema = _resolve_schema(schema, root)
            return schema
    return {}


def _extract_query_params_v2(parameters: list) -> Dict[str, Any]:
    """从 Swagger 2.0 的 parameters 列表中提取 query 参数"""
    params = {}
    for param in parameters:
        if param.get("in") == "query":
            name = param.get("name", "")
            if name:
                params[name] = _make_sample_value(param)
    return params


# ── 主函数 ────────────────────────────────────────────
def generate_cases_from_swagger_data(
    data: dict,
    *,
    include_tags: Optional[List[str]] = None,
    exclude_tags: Optional[List[str]] = None,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    max_cases: int = 0,
    generate_l2: bool = True,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    从 Swagger/OpenAPI 数据字典生成 V2 测试用例

    支持 Swagger 2.0 和 OpenAPI 3.0 两种格式。
    """
    # 检测版本
    is_v2 = data.get("swagger", "").startswith("2") or "swagger" in data

    # 提取 base_url
    servers = data.get("servers", [])
    base_url = servers[0].get("url", "") if servers else ""
    if not base_url:
        host = data.get("host", "")
        base_path = data.get("basePath", "")
        schemes = data.get("schemes", ["https"])
        if host:
            base_url = f"{schemes[0]}://{host}{base_path}"

    paths = data.get("paths", {})
    cases = []
    stats = {"total_apis": 0, "generated": 0, "l2_generated": 0, "skipped": 0, "by_pattern": {}, "by_tag": {}}

    for path, path_info in paths.items():
        for method, info in path_info.items():
            if not isinstance(info, dict):
                continue
            method = method.upper()
            if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue

            stats["total_apis"] += 1
            api_tags = info.get("tags", ["未分类"])
            summary = info.get("summary", "")
            pattern = _detect_pattern(path)

            # Tag 过滤
            if include_tags:
                if not any(t in include_tags for t in api_tags):
                    stats["skipped"] += 1
                    continue
            if exclude_tags:
                if any(t in exclude_tags for t in api_tags):
                    stats["skipped"] += 1
                    continue

            # Pattern 过滤
            if include_patterns and pattern not in include_patterns:
                stats["skipped"] += 1
                continue
            if exclude_patterns and pattern in exclude_patterns:
                stats["skipped"] += 1
                continue

            # 提取 requestBody schema（兼容 v2 和 v3）
            req_schema = {}
            if is_v2:
                parameters = info.get("parameters", [])
                req_schema = _extract_body_schema_v2(parameters, data)
            else:
                rb = info.get("requestBody", {})
                if "$ref" in rb:
                    rb = _resolve_schema(rb, data)
                content = rb.get("content", {})
                if "application/json" in content:
                    req_schema = content["application/json"].get("schema", {})
                    if "$ref" in req_schema:
                        req_schema = _resolve_schema(req_schema, data)

            case = _build_case(
                path=path,
                method=method,
                summary=summary,
                tags=api_tags,
                pattern=pattern,
                req_schema=req_schema,
                responses=info.get("responses", {}),
                root_spec=data,
            )

            # 为 GET 请求添加 query 参数
            if method == "GET" and is_v2:
                qp = _extract_query_params_v2(info.get("parameters", []))
                if qp:
                    case["query_params"] = qp

            cases.append(case)
            stats["generated"] += 1
            stats["by_pattern"][pattern] = stats["by_pattern"].get(pattern, 0) + 1
            for t in api_tags:
                stats["by_tag"][t] = stats["by_tag"].get(t, 0) + 1

            # L2: 参数变异用例
            if generate_l2:
                l2_cases = _build_mutation_cases(
                    path=path, method=method, summary=summary,
                    tags=api_tags, pattern=pattern,
                    req_schema=req_schema, root_spec=data,
                )
                cases.extend(l2_cases)
                stats["l2_generated"] += len(l2_cases)

            if max_cases and len(cases) >= max_cases:
                break
        if max_cases and len(cases) >= max_cases:
            break

    # ── 按接口错误约定重写断言 ──
    # 上面各处的断言模板是硬编码字面量（一律期望 HTTP 4xx）。
    # 如果接口只声明 HTTP 200，异常用例会与该模板断言失配。
    # 这里在出口统一按探测到的约定重写，一处生效，覆盖全部模板。
    convention = _apply_error_convention(cases, data)

    meta = {
        "base_url": base_url,
        "stats": stats,
        "swagger_version": "2.0" if is_v2 else "3.0",
        "tags": sorted(stats["by_tag"].keys()),
        "error_convention": convention,
    }
    return cases, meta


def _apply_error_convention(cases: List[Dict[str, Any]],
                            spec: Dict[str, Any]) -> Dict[str, Any]:
    """就地重写每条用例的断言，使其匹配接口的错误表达方式。

    失败时保持原断言不变——宁可沿用旧模板，也不要让导入整体失败。
    """
    try:
        from trustharness.conventions import infer_from_spec, rewrite_assertions
    except ImportError:
        return {"style": "unknown", "source": "unavailable"}

    try:
        conv = infer_from_spec(spec)
        for case in cases:
            # L1 主用例未显式标注意图，它们测的是正常流程
            intent = case.get("data_type") or "valid"
            case.setdefault("data_type", "valid")
            case["assertions"] = rewrite_assertions(
                case.get("assertions"), conv, intent=intent,
                expected_behavior=case.get("expected_behavior"))
        return conv.to_dict()
    except Exception as exc:  # 生成链路不能因为断言重写而中断
        logger.warning("按错误约定重写断言失败，沿用原模板: %s", exc)
        return {"style": "unknown", "source": "error", "notes": [str(exc)]}


def generate_cases_from_swagger(
    swagger_path: str,
    *,
    include_tags: Optional[List[str]] = None,
    exclude_tags: Optional[List[str]] = None,
    include_patterns: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    max_cases: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    从 Swagger 文件生成 V2 测试用例（兼容旧接口）
    """
    with open(swagger_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return generate_cases_from_swagger_data(
        data,
        include_tags=include_tags,
        exclude_tags=exclude_tags,
        include_patterns=include_patterns,
        exclude_patterns=exclude_patterns,
        max_cases=max_cases,
    )


def fetch_swagger_from_url(url: str, timeout: int = 30) -> dict:
    """从 URL 获取 Swagger JSON 数据"""
    resp = httpx.get(url, timeout=timeout, verify=False)
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError as e:
        content_type = resp.headers.get("content-type", "")
        preview = resp.text[:500].replace("\n", " ").replace("\r", " ")
        raise ValueError(
            f"响应不是 JSON: content-type={content_type}, body_preview={preview}"
        ) from e
    if isinstance(data, dict):
        if "data" in data and isinstance(data["data"], dict) and (
            "swagger" in data["data"] or "openapi" in data["data"] or "paths" in data["data"]
        ):
            return data["data"]
        if "data" in data and isinstance(data["data"], str):
            try:
                parsed = json.loads(data["data"])
                if isinstance(parsed, dict):
                    return parsed
            except ValueError:
                pass
    return data
