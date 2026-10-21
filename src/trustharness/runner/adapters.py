"""Runner adapter contract used by the harness orchestration layer.

The harness owns lifecycle and evidence; concrete tools own execution.  Keeping
this small prevents API, browser and performance runners from leaking their
tool-specific setup into the scheduler.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Protocol, runtime_checkable

from trustharness.runner.execution_engine import ExecutionEngineV2
from trustharness.runner.models import ExecutionResult, TestCaseV2


@dataclass(frozen=True)
class RunnerContext:
    run_id: str
    project_id: Optional[int] = None
    environment_id: Optional[int] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class RunnerAdapter(Protocol):
    """Minimal lifecycle every execution backend must implement."""

    name: str

    def prepare(self, context: RunnerContext) -> None: ...

    def execute(self, case: Any) -> Any: ...

    def collect_result(self, raw_result: Any) -> Any: ...

    def cleanup(self) -> None: ...


class ApiRunnerAdapter:
    """Adapter for the existing real HTTP execution engine."""

    name = "api"

    def __init__(self, *, engine: Optional[ExecutionEngineV2] = None,
                 engine_factory: Optional[Callable[[], ExecutionEngineV2]] = None):
        if engine is None and engine_factory is None:
            raise ValueError("engine or engine_factory is required")
        self._engine = engine
        self._engine_factory = engine_factory
        self._context: Optional[RunnerContext] = None

    def prepare(self, context: RunnerContext) -> None:
        self._context = context
        if self._engine is None:
            self._engine = self._engine_factory()

    def execute(self, case: TestCaseV2) -> ExecutionResult:
        if self._context is None or self._engine is None:
            raise RuntimeError("runner adapter must be prepared before execute")
        return self._engine.execute_case(case, run_id=self._context.run_id)

    def collect_result(self, raw_result: ExecutionResult) -> ExecutionResult:
        return raw_result

    def cleanup(self) -> None:
        self._context = None


class RunnerRegistry:
    """Explicit adapter registry; unknown runner types fail closed."""

    def __init__(self):
        self._factories: Dict[str, Callable[..., RunnerAdapter]] = {}

    def register(self, name: str, factory: Callable[..., RunnerAdapter]) -> None:
        key = (name or "").strip().lower()
        if not key:
            raise ValueError("runner name is required")
        self._factories[key] = factory

    def create(self, name: str, **kwargs: Any) -> RunnerAdapter:
        key = (name or "").strip().lower()
        if key not in self._factories:
            raise ValueError(f"unsupported runner adapter: {name}")
        adapter = self._factories[key](**kwargs)
        if not isinstance(adapter, RunnerAdapter):
            raise TypeError(f"invalid runner adapter: {name}")
        return adapter

    def names(self) -> list[str]:
        return sorted(self._factories)


DEFAULT_RUNNERS = RunnerRegistry()
DEFAULT_RUNNERS.register("api", ApiRunnerAdapter)
