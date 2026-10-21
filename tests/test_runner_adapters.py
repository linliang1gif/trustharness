from types import SimpleNamespace

import pytest

from trustharness.runner.adapters import ApiRunnerAdapter, RunnerContext, RunnerRegistry


class FakeEngine:
    def __init__(self):
        self.calls = []

    def execute_case(self, case, run_id):
        self.calls.append((case, run_id))
        return SimpleNamespace(status="passed")


def test_api_adapter_enforces_lifecycle_and_preserves_run_identity():
    engine = FakeEngine()
    adapter = ApiRunnerAdapter(engine=engine)
    with pytest.raises(RuntimeError):
        adapter.execute(object())

    adapter.prepare(RunnerContext(run_id="RUN-1", project_id=2, environment_id=3))
    case = object()
    result = adapter.collect_result(adapter.execute(case))
    assert result.status == "passed"
    assert engine.calls == [(case, "RUN-1")]
    adapter.cleanup()


def test_registry_fails_closed_for_unknown_runner():
    registry = RunnerRegistry()
    registry.register("api", ApiRunnerAdapter)
    assert registry.names() == ["api"]
    assert isinstance(registry.create("API", engine=FakeEngine()), ApiRunnerAdapter)
    with pytest.raises(ValueError, match="unsupported runner"):
        registry.create("shell")
