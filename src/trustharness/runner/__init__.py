"""
Executor V2 - 真实 HTTP API 测试执行引擎

替代原有 Mock Runner，真正发送 HTTP 请求并执行断言。
"""

from trustharness.runner.execution_engine import ExecutionEngineV2
from trustharness.runner.http_runner import HttpRunner
from trustharness.runner.assertion_engine import AssertionEngineV2
from trustharness.runner.schema_validator import SchemaValidator
from trustharness.runner.result_writer import ResultWriter
from trustharness.runner.auth_manager import AuthManager
from trustharness.runner.adapters import ApiRunnerAdapter, RunnerAdapter, RunnerContext, RunnerRegistry
from trustharness.runner.models import (
    TestCaseV2,
    ExecutionResult,
    AssertionResult,
    AssertionDef,
    HttpRequest,
    HttpResponse,
)

__all__ = [
    "ExecutionEngineV2",
    "HttpRunner",
    "AssertionEngineV2",
    "SchemaValidator",
    "ResultWriter",
    "TestCaseV2",
    "ExecutionResult",
    "AssertionResult",
    "HttpRequest",
    "HttpResponse",
    "AuthManager",
    "ApiRunnerAdapter",
    "RunnerAdapter",
    "RunnerContext",
    "RunnerRegistry",
]
