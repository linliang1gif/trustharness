import pytest

from trustharness.runner.models import TestCaseV2


def _case(path: str, base_url: str = "https://example.test/api") -> TestCaseV2:
    return TestCaseV2(id="case-1", title="case", method="POST", path=path, base_url=base_url)


def test_full_url_keeps_same_origin_absolute_url_without_duplicate_prefix():
    absolute = "https://example.test/api/orders/page"

    assert _case(absolute).full_url == absolute


def test_full_url_joins_relative_path_to_base_url():
    assert _case("/orders/page").full_url == "https://example.test/api/orders/page"


def test_full_url_rejects_cross_origin_absolute_url():
    with pytest.raises(ValueError, match="不属于同一站点"):
        _case("https://other.test/api/orders/page").full_url
