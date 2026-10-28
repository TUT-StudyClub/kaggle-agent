from ..domain.skills import SkillBook
from .dto.approval import ApprovalDecision, ApprovalRequest
from .dto.planning import Plan, PlanningRequest
from .dto.response import ResponseDecision, ResponseRequest
from .ports.planner import Planner
from .use_cases.evaluate import evaluate_japanese
from .use_cases.probe import ProbePlanningUseCase


class PlanningService(Planner):
    def __init__(
        self,
        planner: Planner,
        skillbook: SkillBook,
        probe: ProbePlanningUseCase,
        model: str,
        reasoning_effort: str,
    ):
        self._planner = planner
        self._probe = probe
        self.skillbook = skillbook
        self.model = model
        self.reasoning_effort = reasoning_effort

    def plan(self, request: PlanningRequest) -> Plan:
        return self._planner.plan(request)

    def check_response(self, request: ResponseRequest) -> ResponseDecision:
        return self._planner.check_response(request)

    def check_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return self._planner.check_approval(request)

    def probe(self) -> float:
        return self._probe.execute()

    def evaluate(self) -> list[dict[str, object]]:
        return evaluate_japanese(self._planner)
