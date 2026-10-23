from abc import ABC, abstractmethod

from ..dto.planning import Plan, PlanningRequest
from ..dto.response import ResponseDecision, ResponseRequest


class Planner(ABC):
    @abstractmethod
    def check_response(self, request: ResponseRequest) -> ResponseDecision: ...

    @abstractmethod
    def plan(self, request: PlanningRequest) -> Plan: ...
