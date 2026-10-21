import pytest


def test_not_contains_is_the_inverse_of_contains():
    """not_contains 以前不认识，落到 actual == expected：列表里确实没有的值也判失败（10-12 场景 900）。"""
    pytest.importorskip("httpx")
    from trustharness.runner.assertion_engine import AssertionEngineV2

    fields = ["sellOrderNumber", "purchaseOrderNumber"]
    assert AssertionEngineV2._compare(fields, "orderRemarks", "not_contains") is True
    assert AssertionEngineV2._compare(fields, "purchaseOrderNumber", "not_contains") is False
    assert AssertionEngineV2._compare("记录已提交", "作废", "not_contains") is True
    for actual, expected in ((fields, "orderRemarks"), (fields, "sellOrderNumber"), ("abc", "b")):
        assert AssertionEngineV2._compare(actual, expected, "contains") != \
            AssertionEngineV2._compare(actual, expected, "not_contains")
