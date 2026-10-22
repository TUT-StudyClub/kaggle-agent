from zoneinfo import ZoneInfo

from modules.planning.application.dto.planning import PlanningRequest
from modules.planning.application.ports.planner import Planner
from shared.application.ports.clock import Clock
from shared.errors import AgentError


class ProbePlanningUseCase:
    def __init__(self, planner: Planner, clock: Clock):
        self._planner = planner
        self._clock = clock

    def execute(self) -> float:
        started = self._clock.monotonic()
        plan = self._planner.plan(
            PlanningRequest(
                instruction="検証です。replyで『接続確認』とだけ返してください。ツールは使用しません。",
                now=self._clock.now().astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
                context="{}",
                resources={},
                thread="[]",
                observations=[],
            )
        )
        if plan.action.kind != "reply":
            raise AgentError("probe_failed", "構造化出力の検証に失敗しました。")
        return self._clock.monotonic() - started
