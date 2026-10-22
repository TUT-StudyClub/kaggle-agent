from abc import ABC, abstractmethod

from ..dto.planning import Plan, PlanningRequest


class Planner(ABC):
    @abstractmethod
    def plan(self, request: PlanningRequest) -> Plan: ...
