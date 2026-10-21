"""
Executor V2 - 数据模型

所有执行引擎的输入/输出数据结构。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime
from enum import Enum
from urllib.parse import urlsplit


class HttpMethod(str, Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"


class AssertionType(str, Enum):
    STATUS_CODE = "status_code"
    RESPONSE_TIME = "response_time"
    JSON_PATH = "json_path"
    FIELD_EXISTS = "field_exists"
    FIELD_EQUALS = "field_equals"
    CONTAINS = "contains"
    SCHEMA = "schema"


class ExecutionStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


# ---------- 敏感 Header 脱敏 ----------

SENSITIVE_HEADERS = {
    "authorization", "cookie", "token", "x-token",
    "x-auth-token", "x-api-key", "api-key", "secret",
    "x-csrf-token", "set-cookie",
}


def sanitize_headers(headers: Dict[str, str]) -> Dict[str, str]:
    """对敏感 Header 做脱敏处理"""
    if not headers:
        return {}
    sanitized = {}
    for k, v in headers.items():
        if k.lower() in SENSITIVE_HEADERS:
            sanitized[k] = v[:8] + "***" if len(v) > 8 else "***"
        else:
            sanitized[k] = v
    return sanitized


# ---------- HTTP 请求/响应 ----------

@dataclass
class HttpRequest:
    """记录实际发出的 HTTP 请求"""
    method: str
    url: str
    headers: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, Any] = field(default_factory=dict)
    path_params: Dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_type: str = "json"  # json | form | raw
    timeout: float = 30.0

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "url": self.url,
            "headers": sanitize_headers(self.headers),
            "query_params": self.query_params,
            "path_params": self.path_params,
            "body": self.body,
            "body_type": self.body_type,
            "timeout": self.timeout,
        }


@dataclass
class HttpResponse:
    """记录实际收到的 HTTP 响应"""
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_text: str = ""
    elapsed_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "status_code": self.status_code,
            "headers": sanitize_headers(self.headers),
            "body": self.body,
            "body_text": self.body_text[:5000] if self.body_text else "",
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


# ---------- 断言 ----------

@dataclass
class AssertionDef:
    """断言定义（输入）"""
    type: str  # AssertionType value
    expected: Any = None
    path: str = ""  # json_path 或 field_name
    operator: str = "eq"  # eq, ne, gt, lt, gte, lte, contains, regex
    schema: Optional[dict] = None  # JSON Schema


@dataclass
class AssertionResult:
    """单条断言结果。"""
    type: str
    passed: bool
    expected: Any = None
    actual: Any = None
    message: str = ""
    path: str = ""
    operator: str = "eq"

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "message": self.message,
            "path": self.path,
            "operator": self.operator,
        }


# ---------- 测试用例 ----------

@dataclass
class TestCaseV2:
    """V2 测试用例定义"""
    id: str
    title: str
    method: str
    path: str
    base_url: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    cookies: Dict[str, str] = field(default_factory=dict)
    query_params: Dict[str, Any] = field(default_factory=dict)
    path_params: Dict[str, str] = field(default_factory=dict)
    body: Any = None
    body_type: str = "json"
    timeout: float = 30.0
    assertions: List[AssertionDef] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    description: str = ""
    # 「鉴权缺失」用例：不注入令牌，并摘掉已有的鉴权头。
    # 以前这类用例照样带着令牌发出去，接口正常返回，用例测不到鉴权
    skip_auth: bool = False

    @property
    def full_url(self) -> str:
        path_parts = urlsplit(self.path)
        if path_parts.scheme in {"http", "https"} and path_parts.netloc:
            base_parts = urlsplit(self.base_url)
            if self.base_url and (
                path_parts.scheme.lower(), path_parts.netloc.lower()
            ) != (
                base_parts.scheme.lower(), base_parts.netloc.lower()
            ):
                raise ValueError("绝对请求地址与测试环境不属于同一站点")
            url = self.path
        else:
            url = (self.base_url.rstrip("/") + "/" + self.path.lstrip("/")) if self.base_url else self.path
        # 替换 path params
        for k, v in self.path_params.items():
            url = url.replace(f"{{{k}}}", str(v))
        return url


# ---------- 执行结果 ----------

@dataclass
class ExecutionResult:
    """完整的执行结果"""
    case_id: str
    case_title: str
    status: str  # ExecutionStatus value
    request: Optional[HttpRequest] = None
    response: Optional[HttpResponse] = None
    assertions: List[AssertionResult] = field(default_factory=list)
    error_message: str = ""
    duration_ms: float = 0.0
    started_at: str = ""
    finished_at: str = ""

    @property
    def passed(self) -> bool:
        return self.status == ExecutionStatus.PASSED.value

    @property
    def assertion_summary(self) -> dict:
        total = len(self.assertions)
        passed = sum(1 for a in self.assertions if a.passed)
        return {"total": total, "passed": passed, "failed": total - passed}

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "case_title": self.case_title,
            "status": self.status,
            "request": self.request.to_dict() if self.request else None,
            "response": self.response.to_dict() if self.response else None,
            "assertions": [a.to_dict() for a in self.assertions],
            "assertion_summary": self.assertion_summary,
            "error_message": self.error_message,
            "duration_ms": round(self.duration_ms, 2),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }
