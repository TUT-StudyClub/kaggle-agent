from modules.planning.infrastructure.codex_cli import CodexCLIPlanner
from modules.planning.public import Plan, PlanningRequest
from modules.scheduling.domain.models import Event
from modules.scheduling.public import ListEvents
from scripts.evaluate_research import (
    CASES,
    NOTE,
    PRIVATE_MARKER,
    evaluate_case,
    fixture_result,
)
from tests.support import period


class EvaluationPlanner(CodexCLIPlanner):
    def __init__(self, steps: list[tuple[dict, list[dict]]]):
        self.steps = steps
        self.last_web_searches = []
        self.requests: list[PlanningRequest] = []

    def plan(self, request: PlanningRequest) -> Plan:
        self.requests.append(request)
        action, searches = self.steps.pop(0)
        self.last_web_searches = searches
        return Plan.model_validate({"action": action})


def case(name):
    return next(item for item in CASES if item.name == name)


def test_evaluation_requires_actual_search_events_not_a_claim_with_a_link():
    planner = EvaluationPlanner(
        [
            (
                {
                    "kind": "reply",
                    "text": "python -m venv .venv; activate https://docs.python.org/3/library/venv.html",
                },
                [],
            )
        ]
    )
    result = evaluate_case(planner, case("public_documentation"))
    assert not result["passed"]
    assert any("実行イベント" in error for error in result["errors"])


def test_evaluation_replays_record_then_web_and_checks_original_question():
    planner = EvaluationPlanner(
        [
            ({"kind": "notion.read", "page_id": NOTE}, []),
            (
                {
                    "kind": "reply",
                    "text": "Python 3.14: python -m venv .venv https://docs.python.org/3.14/library/venv.html",
                },
                [{"type": "search", "query": "Python 3.14 venv site:docs.python.org"}],
            ),
        ]
    )
    result = evaluate_case(planner, case("record_to_public"))
    assert result["passed"], result
    assert result["steps"] == ["notion.read", "reply"]
    page = planner.requests[1].observations[0]["page"]
    assert isinstance(page, dict)
    assert "3.14" in page["text"]


def test_evaluation_rejects_unneeded_calendar_and_wrong_competition_database():
    for action in [
        {"kind": "calendar.list"},
        {"kind": "notion.read", "page_id": "00000000-0000-0000-0000-000000000099"},
    ]:
        result = evaluate_case(EvaluationPlanner([(action, [])]), case("internal_record"))
        assert not result["passed"]


def test_evaluation_rejects_private_marker_in_web_search():
    planner = EvaluationPlanner(
        [
            (
                {
                    "kind": "reply",
                    "text": "python -m venv .venv; activate https://docs.python.org/3/library/venv.html",
                },
                [{"type": "search", "query": PRIVATE_MARKER}],
            )
        ]
    )
    result = evaluate_case(planner, case("public_documentation"))
    assert not result["passed"]
    assert any("非公開" in error for error in result["errors"])


def test_calendar_fixture_uses_fields_really_available_in_application():
    result = fixture_result(
        case("calendar_to_public"), ListEvents(calendar_alias="personal", period=period())
    )
    raw = {key: value for key, value in result["events"][0].items() if key != "member"}
    event = Event.model_validate(raw)
    assert event.location == "東京国際フォーラム"


def test_missing_private_source_can_search_records_but_cannot_invent_a_bill():
    planner = EvaluationPlanner(
        [
            ({"kind": "notion.search", "query": "AWS 今月 請求"}, []),
            (
                {
                    "kind": "reply",
                    "text": "AWSの請求記録がなく、請求システムへのアクセスもないため確認できない。",
                },
                [],
            ),
        ]
    )
    result = evaluate_case(planner, case("unavailable_private_source"))
    assert result["passed"], result
    assert planner.requests[1].observations == [{"pages": []}]
    invented = EvaluationPlanner(
        [({"kind": "reply", "text": "AWSの請求システムへアクセスできないけど、今月は123円。"}, [])]
    )
    result = evaluate_case(invented, case("unavailable_private_source"))
    assert not result["passed"]
    assert any("数値" in error for error in result["errors"])
