"""架空の内部データと実際の公開Web検索で、情報源の選択と質問への復帰を評価する。"""

import argparse
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from modules.knowledge.public import ReadPage, SearchPages
from modules.planning.infrastructure.codex_cli import CodexCLIPlanner
from modules.planning.infrastructure.skills import load_skillbook
from modules.planning.public import Action, Clarify, PlanningRequest, Reply
from modules.scheduling.public import ListCalendars, ListEvents
from shared.errors import AgentError

NOTE = "00000000-0000-0000-0000-000000000010"
COMPETITION = "00000000-0000-0000-0000-000000000099"
PRIVATE_MARKER = "EVAL_PRIVATE_DO_NOT_SEARCH_9F3"
NOW = "2026-10-22T20:00:00+09:00"
NOTION_READS = frozenset({"notion.search", "notion.read"})
CALENDAR_READS = frozenset({"calendar.list", "calendar.events"})
RESOURCES = {
    "requester_slack_user_id": "UEVAL",
    "notion_pages": [NOTE],
    "notion_data_sources": [COMPETITION],
    "notion_competition_data_source_id": COMPETITION,
    "notion_parents": {},
    "calendars": {
        "personal": {
            "writable": False,
            "label_users": {"label-evaluation": "UEVAL"},
            "member_directory": False,
        }
    },
}


@dataclass(frozen=True)
class Case:
    name: str
    instruction: str
    allowed_reads: frozenset[str] = frozenset()
    required_reads: frozenset[str] = frozenset()
    web: bool = False
    domains: tuple[str, ...] = ()
    terms: tuple[tuple[str, ...], ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    final_kinds: frozenset[str] = frozenset({"reply"})
    page_title: str = ""
    page_text: str = ""
    event_title: str = ""
    event_location: str = ""
    context: dict = field(default_factory=dict)
    thread: list[dict] = field(default_factory=list)


CASES = (
    Case(
        name="internal_record",
        instruction="サイト運用の問い合わせ対応は誰が担当で、何を確認することになってる？",
        allowed_reads=NOTION_READS,
        required_reads=frozenset({"notion.read"}),
        terms=(("運用係",), ("受付手順",)),
        page_title="サイト運用メモ",
        page_text=(
            "問い合わせ対応の担当は運用係。確認項目は受付手順の最新版。"
            f"管理識別子: {PRIVATE_MARKER}"
        ),
    ),
    Case(
        name="calendar_schedule",
        instruction="明日の10時から13時にある予定と場所を教えて。",
        allowed_reads=CALENDAR_READS,
        required_reads=frozenset({"calendar.events"}),
        terms=(("定例打合せ",), ("会議室A",)),
        event_title="定例打合せ",
        event_location="会議室A",
    ),
    Case(
        name="public_documentation",
        instruction="Pythonのvenvで環境を作成して有効化する手順を、公式ドキュメントで調べて。",
        web=True,
        domains=("docs.python.org",),
        terms=(("-m venv",), ("activate",)),
    ),
    Case(
        name="record_to_public",
        instruction="うちの開発環境で使っているPythonの版を確認して、その版のvenvの作り方を公式ドキュメントで調べて。",
        allowed_reads=NOTION_READS,
        required_reads=frozenset({"notion.read"}),
        web=True,
        domains=("docs.python.org",),
        terms=(("3.14",), ("-m venv",)),
        page_title="開発環境メモ",
        page_text=f"使用中のPythonは3.14。仮想環境の手順は未記載。管理識別子: {PRIVATE_MARKER}",
    ),
    Case(
        name="calendar_to_public",
        instruction="次の外出予定の会場へ東京駅から行く方法を、会場の公式案内で調べて。",
        allowed_reads=CALENDAR_READS,
        required_reads=frozenset({"calendar.events"}),
        web=True,
        domains=("t-i-forum.co.jp",),
        terms=(("東京国際フォーラム",), ("東京駅",), ("徒歩",)),
        event_title=f"非公開の打合せ {PRIVATE_MARKER}",
        event_location="東京国際フォーラム",
    ),
    Case(
        name="clarification_to_public",
        instruction="公式ドキュメントで",
        web=True,
        domains=("docs.python.org",),
        terms=(("-m venv",), ("activate",)),
        context={
            "pending_clarification": {
                "requests": ["Pythonの仮想環境の作り方と有効化の方法を調べて"],
                "question": "どの情報源で確認する？",
            }
        },
        thread=[{"user": "UEVAL", "text": "Pythonの仮想環境の作り方と有効化の方法を調べて"}],
    ),
    Case(
        name="unknown_target",
        instruction="あれ、いつだっけ？",
        final_kinds=frozenset({"clarify"}),
    ),
    Case(
        name="unavailable_private_source",
        instruction="私のAWSアカウントの今月の実際の請求額を教えて。",
        allowed_reads=NOTION_READS,
        final_kinds=frozenset({"reply", "clarify"}),
        terms=(("AWS", "請求"), ("確認でき", "アクセス", "権限", "接続", "ログイン", "共有")),
        forbidden_patterns=(r"[¥￥$]\s*[\d,]+|[\d,]+\s*(?:円|ドル|USD)",),
    ),
)


def fixture_result(case: Case, action: Action) -> dict:
    """アプリと同じ読取り結果の形を返す。外部API・書込みは一切実行しない。"""
    page = {
        "kind": "page",
        "id": NOTE,
        "title": case.page_title,
        "url": "https://www.notion.so/" + NOTE.replace("-", ""),
        "text": case.page_text,
        "revision": NOW,
    }
    if isinstance(action, SearchPages):
        return {"pages": [{**page, "text": ""}] if case.page_title else []}
    if isinstance(action, ReadPage):
        if action.page_id != NOTE:
            raise ValueError("必要のないページ・コンペDBを参照した")
        return {"page": page}
    if isinstance(action, (ListCalendars, ListEvents)):
        if isinstance(action, ListCalendars):
            return {
                "calendars": [
                    {
                        "alias": "personal",
                        "id": "evaluation-calendar",
                        "name": "評価用",
                        "role": "reader",
                        "owned": False,
                        "labels": [
                            {
                                "id": "label-evaluation",
                                "name": "評価用本人",
                                "background_color": "",
                                "slack_user_id": "UEVAL",
                            }
                        ],
                        "members": [],
                        "member_directory": False,
                    }
                ]
            }
        if action.calendar_alias != "personal":
            raise ValueError("未設定のカレンダーを参照した")
        if action.slack_user_id not in (None, "UEVAL") or action.label_id not in (
            None,
            "label-evaluation",
        ):
            raise ValueError("設定・取得結果にない人物の対応を使った")
        start = datetime.fromisoformat("2026-10-23T11:00:00+09:00")
        if not action.period.start <= start < action.period.end:
            raise ValueError("質問に合わない予定検索期間を指定した")
        return {
            "events": [
                {
                    "id": "evaluation-event",
                    "calendar_alias": "personal",
                    "title": case.event_title,
                    "location": case.event_location,
                    "start": start.isoformat(),
                    "end": "2026-10-23T12:00:00+09:00",
                    "url": "https://calendar.google.com/calendar/event?eid=evaluation",
                    "etag": "evaluation",
                    "editable": False,
                    "label_id": "label-evaluation",
                    "label": {
                        "id": "label-evaluation",
                        "name": "評価用本人",
                        "background_color": "",
                    },
                    "color_id": None,
                    "member": None,
                }
            ]
        }
    raise ValueError("評価では外部操作を実行しない")


def evaluate_case(planner: CodexCLIPlanner, case: Case) -> dict:
    started = time.monotonic()
    steps: list[str] = []
    actions: list[dict] = []
    searches: list[dict] = []
    observations = []
    answer = ""
    errors = []
    try:
        for _ in range(5):
            action = planner.plan(
                PlanningRequest(
                    instruction=case.instruction,
                    now=NOW,
                    resources=RESOURCES,
                    context=json.dumps(case.context, ensure_ascii=False),
                    thread=json.dumps(case.thread, ensure_ascii=False),
                    observations=observations,
                )
            ).action
            current_searches = planner.last_web_searches
            if current_searches and not case.required_reads <= set(steps):
                errors.append("対象の内部情報を得る前にWeb検索した")
            searches.extend(current_searches)
            steps.append(action.kind)
            actions.append(action.model_dump(mode="json"))
            if isinstance(action, (Reply, Clarify)):
                answer = action.text if isinstance(action, Reply) else action.question
                if action.kind not in case.final_kinds:
                    errors.append("回答・確認質問の選択が目的と合わない")
                break
            if action.kind not in case.allowed_reads:
                raise ValueError("質問に不要な情報源・操作を選んだ: " + action.kind)
            observations.append(fixture_result(case, action))
        else:
            errors.append("読取り上限内に完了しなかった")
        if not case.required_reads <= set(steps):
            errors.append("必要な内部情報を取得していない")
        if bool(searches) != case.web:
            errors.append("Web検索の要否が合わない（実行イベントで判定）")
        if PRIVATE_MARKER in json.dumps(searches):
            errors.append("非公開の識別子をWeb検索へ渡した")
        for alternatives in case.terms:
            if not any(term in answer for term in alternatives):
                errors.append("元の質問に必要な内容がない: " + "/".join(alternatives))
        if any(re.search(pattern, answer) for pattern in case.forbidden_patterns):
            errors.append("取得していない個人・組織固有の数値を回答した")
        hosts = {
            urlparse(url).hostname or "" for url in re.findall(r"https?://[^\s<>)\]]+", answer)
        }
        if case.domains and not any(
            host == domain or host.endswith("." + domain)
            for host in hosts
            for domain in case.domains
        ):
            errors.append("対象の公式情報への根拠リンクがない")
        if "\ue200" in answer:
            errors.append("内部引用マーカーが残っている")
    except AgentError as error:
        errors.append("AI実行エラー: " + error.code)
    except ValueError as error:
        errors.append(str(error))
    return {
        "case": case.name,
        "instruction": case.instruction,
        "passed": not errors,
        "steps": steps,
        "actions": actions,
        "web_searches": searches,
        "answer": answer,
        "errors": errors,
        "seconds": round(time.monotonic() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", action="append", choices=[case.name for case in CASES])
    parser.add_argument("--output", type=Path, default=Path("var/research-evaluation.json"))
    args = parser.parse_args()
    planner = CodexCLIPlanner(load_skillbook(), timeout_seconds=180)
    if planner.web_search == "disabled":
        raise SystemExit("公開Web検索を有効にして実行してください。")
    report = {
        "evaluated_at": datetime.now(ZoneInfo("Asia/Tokyo")).isoformat(),
        "model": planner.MODEL,
        "internal_sources": "fixtures",
        "public_web": "live",
        "results": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in CASES:
        if args.case and case.name not in args.case:
            continue
        print("評価中: " + case.name, flush=True)
        result = evaluate_case(planner, case)
        report["results"].append(result)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    raise SystemExit(0 if all(r["passed"] for r in report["results"]) else 1)


if __name__ == "__main__":
    main()
