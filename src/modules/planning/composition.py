from shared.application.ports.clock import Clock
from shared.infrastructure.clock import SystemClock

from .application.facade import PlanningService
from .application.use_cases.probe import ProbePlanningUseCase
from .infrastructure.codex_cli import CodexCLIPlanner
from .infrastructure.skills import load_skillbook


def build_planning(timeout_seconds: int = 120, *, clock: Clock | None = None) -> PlanningService:
    clock = clock or SystemClock()
    book = load_skillbook()
    planner = CodexCLIPlanner(book, timeout_seconds)
    planner.check_auth()
    return PlanningService(
        planner, book, ProbePlanningUseCase(planner, clock), planner.MODEL, planner.REASONING_EFFORT
    )
