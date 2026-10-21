"""
Executor V2 - 执行引擎主入口

编排：TestCaseV2 → HttpRunner → AssertionEngine → ResultWriter
"""

import logging

logger = logging.getLogger(__name__)

import uuid
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from trustharness.runner.models import (
    AssertionDef,
    AssertionType,
    ExecutionResult,
    ExecutionStatus,
    TestCaseV2,
)
from trustharness.runner.http_runner import HttpRunner
from trustharness.runner.assertion_engine import AssertionEngineV2
from trustharness.runner.result_writer import ResultWriter
from trustharness.runner.auth_manager import AuthManager
from trustharness.runner.env_guard import EnvGuard


class ExecutionEngineV2:
    """
    V2 测试执行引擎 — 真实 HTTP 执行 + 真实断言 + 持久化

    用法:
        engine = ExecutionEngineV2(base_url="http://your-api-server:8080")
        result = engine.execute_case(case)
        results = engine.execute_cases(cases)
    """

    def __init__(
        self,
        base_url: str = "",
        default_timeout: float = 30.0,
        verify_ssl: bool = False,
        db_path: str = "output/execution_results.db",
        auth_env_key: str = "default",
        env_name: str = "",
    ):
        if not base_url:
            raise ValueError("未配置测试环境地址，无法执行真实接口测试。请设置 base_url。")

        self.base_url = base_url.rstrip("/")
        self.auth_env_key = auth_env_key
        self.env_name = env_name
        self.http_runner = HttpRunner(default_timeout=default_timeout, verify_ssl=verify_ssl)
        self.assertion_engine = AssertionEngineV2()
        self.result_writer = ResultWriter(db_path=db_path)

    def execute_case(self, case: TestCaseV2, run_id: str = "") -> ExecutionResult:
        """
        执行单个测试用例。

        1. 补全 base_url
        2. 发送 HTTP 请求
        3. 执行断言
        4. 写入数据库
        5. 返回 ExecutionResult
        """
        # 补全 base_url
        if not case.base_url:
            case.base_url = self.base_url

        # 生产环境保护检查
        allowed, risk_level, guard_msg = EnvGuard.check_permission(
            base_url=case.base_url,
            method=case.method,
            path=case.path,
            summary=case.title,
            env_name=self.env_name,
            force=getattr(case, 'force', False),
        )
        if not allowed:
            return ExecutionResult(
                case_id=case.id,
                case_title=case.title,
                status=ExecutionStatus.ERROR.value,
                error_message=guard_msg,
                duration_ms=0,
                started_at=datetime.now().isoformat(),
                finished_at=datetime.now().isoformat(),
            )

        # 注入认证信息
        headers = dict(case.headers) if case.headers else {}
        cookies = dict(case.cookies) if case.cookies else {}
        if getattr(case, "skip_auth", False):
            headers, cookies = AuthManager.strip_auth(headers, cookies, self.auth_env_key)
        else:
            headers, cookies = AuthManager.inject_auth(headers, cookies, self.auth_env_key)
        case.headers = headers
        case.cookies = cookies

        started_at = datetime.now().isoformat()
        start_time = time.time()

        try:
            # 发送请求
            req, resp = self.http_runner.execute(case)

            # 如果连接失败 (status_code == 0)
            if resp.status_code == 0:
                elapsed = (time.time() - start_time) * 1000
                result = ExecutionResult(
                    case_id=case.id,
                    case_title=case.title,
                    status=ExecutionStatus.ERROR.value,
                    request=req,
                    response=resp,
                    error_message=resp.body_text,
                    duration_ms=elapsed,
                    started_at=started_at,
                    finished_at=datetime.now().isoformat(),
                )
                self.result_writer.write(result, run_id)
                return result

            # 如果没有自定义断言，添加默认断言
            assertions = case.assertions
            if not assertions:
                assertions = self._default_assertions()

            # 执行断言
            assertion_results = self.assertion_engine.run_assertions(assertions, resp)

            # 判断状态
            all_passed = all(a.passed for a in assertion_results)
            elapsed = (time.time() - start_time) * 1000

            result = ExecutionResult(
                case_id=case.id,
                case_title=case.title,
                status=ExecutionStatus.PASSED.value if all_passed else ExecutionStatus.FAILED.value,
                request=req,
                response=resp,
                assertions=assertion_results,
                duration_ms=elapsed,
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
            )

        except ValueError as e:
            # base_url 未配置等
            elapsed = (time.time() - start_time) * 1000
            result = ExecutionResult(
                case_id=case.id,
                case_title=case.title,
                status=ExecutionStatus.ERROR.value,
                error_message=str(e),
                duration_ms=elapsed,
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
            )

        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            result = ExecutionResult(
                case_id=case.id,
                case_title=case.title,
                status=ExecutionStatus.ERROR.value,
                error_message=f"执行引擎异常: {e}",
                duration_ms=elapsed,
                started_at=started_at,
                finished_at=datetime.now().isoformat(),
            )

        # 持久化
        self.result_writer.write(result, run_id)
        return result

    def execute_cases(
        self,
        cases: List[TestCaseV2],
        run_id: str = "",
    ) -> List[ExecutionResult]:
        """批量执行测试用例"""
        if not run_id:
            run_id = f"run-{uuid.uuid4().hex[:8]}"

        results = []
        for case in cases:
            result = self.execute_case(case, run_id=run_id)
            results.append(result)
            logger.info(f"  [{result.status.upper():>6}] {case.title} ({result.duration_ms:.0f}ms)")

        # 打印汇总
        total = len(results)
        passed = sum(1 for r in results if r.passed)
        failed = total - passed
        logger.info(f"\n  汇总: {passed}/{total} 通过, {failed} 失败")

        return results

    def get_run_results(self, run_id: str) -> List[dict]:
        """获取某次运行的全部结果"""
        return self.result_writer.get_by_run(run_id)

    def get_run_summary(self, run_id: str) -> dict:
        """获取某次运行的统计摘要"""
        return self.result_writer.get_summary(run_id)

    def get_result_detail(self, record_id: int) -> Optional[dict]:
        """获取单条结果详情"""
        return self.result_writer.get_by_id(record_id)

    # ------------------------------------------------------------------

    @staticmethod
    def _default_assertions() -> List[AssertionDef]:
        """默认断言：状态码 2xx + 响应时间 < 10s"""
        return [
            AssertionDef(
                type=AssertionType.STATUS_CODE.value,
                expected=[200, 201, 204],
            ),
            AssertionDef(
                type=AssertionType.RESPONSE_TIME.value,
                expected=10000,
            ),
        ]
