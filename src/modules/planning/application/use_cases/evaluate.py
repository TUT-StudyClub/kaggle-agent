import json
import time

from modules.scheduling.public import CreateEvent, ListEvents, UpdateEvent

from ..dto.planning import Clarify, PlanningRequest
from ..ports.planner import Planner


def evaluate_japanese(planner: Planner) -> list[dict[str, object]]:
    """架空のリソースだけで意図判定を検証する。操作案は一切実行しない。"""
    context = json.dumps(
        {
            "event": {
                "id": "evaluation_event",
                "calendar_alias": "personal",
                "title": "面談",
                "start": "2026-10-23T18:30:00+09:00",
                "end": "2026-10-23T19:00:00+09:00",
            }
        }
    )
    cases = [
        (
            "create",
            "2026年10月23日18時半から19時に、personalカレンダーに面談を作成して",
            "{}",
            "[]",
        ),
        ("follow_up", "さっきの面談を30分後ろに変更して。長さは変えないで", context, "[]"),
        ("clarify", "面談を入れて", "{}", "[]"),
        (
            "clarification_follow_up",
            "カレンダー",
            json.dumps(
                {
                    "pending_clarification": {
                        "requests": ["次のミーティングいつ？", "キックオフ"],
                        "question": "どこで確認しますか？",
                    }
                },
                ensure_ascii=False,
            ),
            json.dumps(
                [
                    {"user": "evaluation_user", "ts": "1", "text": "次のミーティングいつ？"},
                    {"user": "evaluation_user", "ts": "2", "text": "キックオフ"},
                    {"user": "evaluation_user", "ts": "3", "text": "カレンダー"},
                ],
                ensure_ascii=False,
            ),
        ),
    ]
    results: list[dict[str, object]] = []
    for name, instruction, previous, thread in cases:
        started = time.monotonic()
        action = planner.plan(
            PlanningRequest(
                instruction=instruction,
                now="2026-10-19T21:00:00+09:00",
                context=previous,
                resources={"calendars": {"personal": {"writable": True}}},
                thread=thread,
                observations=[],
            )
        ).action
        passed = False
        if name == "clarify":
            passed = isinstance(action, Clarify)
        elif name == "create" and isinstance(action, CreateEvent):
            passed = (
                action.calendar_alias == "personal"
                and action.draft.title == "面談"
                and action.draft.period.start.isoformat() == "2026-10-23T18:30:00+09:00"
                and action.draft.period.end.isoformat() == "2026-10-23T19:00:00+09:00"
            )
        elif name == "follow_up" and isinstance(action, UpdateEvent):
            passed = (
                action.calendar_alias == "personal"
                and action.event_id == "evaluation_event"
                and action.draft.period.start.isoformat() == "2026-10-23T19:00:00+09:00"
                and action.draft.period.end.isoformat() == "2026-10-23T19:30:00+09:00"
            )
        elif name == "clarification_follow_up" and isinstance(action, ListEvents):
            passed = (
                action.calendar_alias == "personal"
                and action.period.start.isoformat() == "2026-10-19T21:00:00+09:00"
                and action.period.end.isoformat() == "2026-11-18T21:00:00+09:00"
            )
        results.append(
            {
                "case": name,
                "kind": action.kind,
                "passed": passed,
                "seconds": round(time.monotonic() - started, 1),
            }
        )
    return results
