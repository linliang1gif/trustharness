"""
Executor V2 - HTTP Runner

使用 httpx 真实发送 HTTP 请求。
支持 GET / POST / PUT / DELETE / PATCH。
支持 JSON body / form body / query params / path params / headers / cookies / timeout。
"""

import time
from datetime import datetime
from typing import Any, Dict, Optional

import httpx

from trustharness.runner.models import (
    HttpRequest,
    HttpResponse,
    TestCaseV2,
)


class HttpRunner:
    """真实 HTTP 请求执行器"""

    def __init__(self, default_timeout: float = 30.0, verify_ssl: bool = False):
        self.default_timeout = default_timeout
        self.verify_ssl = verify_ssl

    def execute(self, case: TestCaseV2) -> tuple:
        """
        执行一个测试用例的 HTTP 请求。

        Returns:
            (HttpRequest, HttpResponse)  成功时
            (HttpRequest, None)          请求级别异常时（由调用方处理 error_message）

        Raises:
            ValueError: base_url 未配置
        """
        url = case.full_url
        if not url or not url.startswith("http"):
            raise ValueError(
                "未配置测试环境地址，无法执行真实接口测试。"
                f" (base_url={case.base_url!r}, path={case.path!r})"
            )

        method = case.method.upper()
        timeout = case.timeout or self.default_timeout

        # 合并 headers
        headers = dict(case.headers)
        if case.cookies:
            cookie_str = "; ".join(f"{k}={v}" for k, v in case.cookies.items())
            headers["Cookie"] = cookie_str

        # 构建记录对象
        req = HttpRequest(
            method=method,
            url=url,
            headers=dict(headers),
            query_params=dict(case.query_params),
            path_params=dict(case.path_params),
            body=case.body,
            body_type=case.body_type,
            timeout=timeout,
        )

        # 构建 httpx 请求参数
        request_kwargs: Dict[str, Any] = {
            "method": method,
            "url": url,
            "headers": headers,
            "params": case.query_params or None,
            "timeout": timeout,
        }

        # body
        if case.body is not None:
            if case.body_type == "form":
                request_kwargs["data"] = case.body
            elif case.body_type == "raw":
                request_kwargs["content"] = case.body if isinstance(case.body, (str, bytes)) else str(case.body)
            else:
                # 默认 json
                request_kwargs["json"] = case.body

        # 发送请求
        try:
            if not hasattr(self, '_client') or self._client is None:
                self._client = httpx.Client(verify=self.verify_ssl)
            r = self._client.request(**request_kwargs)

            resp = HttpResponse(
                status_code=r.status_code,
                headers=dict(r.headers),
                body=self._try_parse_json(r),
                body_text=r.text[:10000],
                elapsed_ms=r.elapsed.total_seconds() * 1000,
            )
            return req, resp

        except httpx.TimeoutException as e:
            req_out = req
            return req_out, HttpResponse(
                status_code=0,
                body_text=f"请求超时: {e}",
                elapsed_ms=timeout * 1000,
            )
        except httpx.ConnectError as e:
            return req, HttpResponse(
                status_code=0,
                body_text=f"连接失败: {e}",
                elapsed_ms=0,
            )
        except Exception as e:
            return req, HttpResponse(
                status_code=0,
                body_text=f"请求异常: {e}",
                elapsed_ms=0,
            )

    # ------------------------------------------------------------------

    @staticmethod
    def _try_parse_json(r: httpx.Response) -> Any:
        """尝试解析响应体为 JSON，失败则返回 None"""
        try:
            return r.json()
        except Exception:
            return None
