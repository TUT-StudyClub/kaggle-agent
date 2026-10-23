import hashlib
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from modules.scheduling.domain.models import (
    Calendar,
    Event,
    EventDraft,
    EventLabel,
    TimeRange,
)
from modules.scheduling.domain.repositories import CalendarRepository
from shared.errors import AgentError, UncertainWrite
from shared.infrastructure.http import JsonHttp


class GoogleCalendarRepository(CalendarRepository):
    def __init__(self, http: JsonHttp, calendars: dict[str, str]):
        self.http = http
        self.ids = calendars

    def _id(self, alias: str) -> str:
        if alias not in self.ids:
            raise AgentError("unknown_calendar", "許可されたカレンダーの別名を指定してください。")
        return quote(self.ids[alias], safe="")

    def _path(self, alias: str, event_id: str | None = None) -> str:
        path = f"calendars/{self._id(alias)}/events"
        return f"{path}/{quote(event_id, safe='')}" if event_id else path

    def _labels(self, alias: str) -> dict[str, EventLabel]:
        try:
            raw = self.http.request("GET", f"calendars/{self._id(alias)}")
        except AgentError as error:
            if error.code == "permission":
                raise AgentError(
                    "calendar_labels_permission",
                    "予定ラベルを取得できません。calendar.calendars.readonly権限を確認し、"
                    "必要ならtask auth:googleで再認証してください。",
                ) from None
            raise
        return {
            label["id"]: EventLabel(
                id=label["id"],
                name=label.get("name") or "",
                background_color=label.get("backgroundColor", ""),
            )
            for label in raw.get("labelProperties", {}).get("eventLabels", [])
        }

    def calendars(self) -> list[Calendar]:
        result = []
        for alias, calendar_id in self.ids.items():
            raw = self.http.request("GET", f"users/me/calendarList/{self._id(alias)}")
            role = raw.get("accessRole", "none")
            result.append(
                Calendar(
                    alias=alias,
                    id=calendar_id,
                    name=raw.get("summary", alias),
                    role=role,
                    owned=role == "owner",
                    labels=list(self._labels(alias).values()),
                )
            )
        return result

    def _event(self, alias: str, raw: dict, labels: dict[str, EventLabel] | None = None) -> Event:
        def date_text(value: dict) -> str:
            if "dateTime" in value:
                return (
                    datetime.fromisoformat(value["dateTime"])
                    .astimezone(
                        ZoneInfo("Asia/Tokyo"),
                    )
                    .isoformat()
                )
            return value.get("date", "")

        label_id = raw.get("eventLabelId") or None
        if label_id and labels is None:
            labels = self._labels(alias)
        creator = raw.get("creator", {}).get("email") or None
        guests = [a for a in raw.get("attendees", []) if not a.get("resource", False)]
        return Event(
            id=raw["id"],
            calendar_alias=alias,
            title=raw.get("summary", "(非公開)"),
            location=raw.get("location", ""),
            start=date_text(raw.get("start", {})),
            end=date_text(raw.get("end", {})),
            url=raw.get("htmlLink", ""),
            etag=raw.get("etag", ""),
            label_id=label_id,
            label=(labels or {}).get(label_id) if label_id else None,
            color_id=raw.get("colorId") or None,
            creator_email=creator,
            organizer_email=raw.get("organizer", {}).get("email") or None,
            guest_emails=frozenset(a["email"] for a in guests if a.get("email")),
            attendees_complete=(
                not raw.get("attendeesOmitted", False) and all(a.get("email") for a in guests)
            ),
            participants_complete=(
                bool(creator)
                and not raw.get("attendeesOmitted", False)
                and all(a.get("email") for a in guests)
            ),
            blocks_time=raw.get("transparency") != "transparent",
            editable=(
                raw.get("organizer", {}).get("self", False)
                and not raw.get("locked", False)
                and raw.get("eventType", "default") == "default"
                and not raw.get("recurringEventId")
                and not raw.get("recurrence")
            ),
        )

    def events(self, alias: str, period: TimeRange) -> list[Event]:
        params = {
            "timeMin": period.start.isoformat(),
            "timeMax": period.end.isoformat(),
            "timeZone": "Asia/Tokyo",
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 100,
            "eventLabelVersion": 1,
        }
        result = []
        labels = None
        for _ in range(10):
            raw = self.http.request("GET", self._path(alias), params=params)
            if labels is None and any(
                e.get("eventLabelId") and e.get("status") != "cancelled"
                for e in raw.get("items", [])
            ):
                labels = self._labels(alias)
            result.extend(
                self._event(alias, e, labels)
                for e in raw.get("items", [])
                if e.get("status") != "cancelled"
            )
            if not raw.get("nextPageToken"):
                return result
            params["pageToken"] = raw["nextPageToken"]
        raise AgentError("too_large", "予定が多すぎます。期間を絞ってください。")

    def busy(self, aliases: list[str], period: TimeRange) -> list[TimeRange]:
        for alias in aliases:
            self._id(alias)
        ids = {self.ids[alias] for alias in aliases}
        raw = self.http.request(
            "POST",
            "freeBusy",
            read=True,
            json={
                "timeMin": period.start.isoformat(),
                "timeMax": period.end.isoformat(),
                "timeZone": "Asia/Tokyo",
                "items": [{"id": value} for value in sorted(ids)],
            },
        )
        calendars = raw.get("calendars", {})
        if any(value not in calendars or calendars[value].get("errors") for value in ids):
            raise AgentError("busy_unavailable", "一部カレンダーのBusyを取得できません。")
        return [
            TimeRange.model_validate(slot)
            for value in ids
            for slot in calendars[value].get("busy", [])
        ]

    def event(self, alias: str, event_id: str) -> Event:
        raw = self.http.request("GET", self._path(alias, event_id), params={"eventLabelVersion": 1})
        if raw.get("status") == "cancelled":
            raise AgentError("not_found", "予定は削除済みです。")
        return self._event(alias, raw)

    def event_key(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()

    def find_applied(self, alias: str, event_id: str | None, key: str) -> Event | None:
        try:
            raw = self.http.request(
                "GET",
                self._path(alias, event_id or self.event_key(key)),
                params={"eventLabelVersion": 1},
            )
        except AgentError as error:
            if error.code == "not_found":
                return None
            raise
        if (
            raw.get("status") == "cancelled"
            or raw.get("extendedProperties", {}).get("private", {}).get("agentKey") != key
        ):
            return None
        return self._event(alias, raw)

    def _body(self, draft: EventDraft, key: str) -> dict:
        return {
            "summary": draft.title,
            "start": {"dateTime": draft.period.start.isoformat(), "timeZone": "Asia/Tokyo"},
            "end": {"dateTime": draft.period.end.isoformat(), "timeZone": "Asia/Tokyo"},
            "extendedProperties": {"private": {"agentKey": key}},
        }

    def create(self, alias: str, draft: EventDraft, key: str) -> Event:
        event_id = self.event_key(key)
        existing = self.find_applied(alias, None, key)
        if existing is not None:
            self._verify(existing, draft)
            return existing
        body = self._body(draft, key) | {"id": event_id}
        if draft.label is not None:
            body["eventLabelId"] = draft.label.id
        if draft.guest_emails:
            body["attendees"] = [{"email": email} for email in draft.guest_emails]
        self.http.request(
            "POST",
            self._path(alias),
            json=body,
            params={
                "sendUpdates": "all" if draft.guest_emails else "none",
                "eventLabelVersion": 1,
            },
        )
        result = self.event(alias, event_id)
        self._verify(result, draft)
        return result

    def _verify(self, result: Event, draft: EventDraft) -> None:
        if (
            result.title != draft.title
            or datetime.fromisoformat(result.start) != draft.period.start
            or datetime.fromisoformat(result.end) != draft.period.end
            or (draft.label is not None and result.label != draft.label)
            or (
                draft.guest_emails
                and (
                    not result.attendees_complete
                    or {email.lower() for email in result.guest_emails} != set(draft.guest_emails)
                )
            )
        ):
            raise UncertainWrite()

    def update(self, alias: str, event: Event, draft: EventDraft, key: str) -> Event:
        # PATCHで指定しないゲスト・出欠回答・会議情報などは既存の値を保持する。
        self.http.request(
            "PATCH",
            self._path(alias, event.id),
            json=self._body(draft, key),
            headers={"If-Match": event.etag},
            params={"sendUpdates": "none", "eventLabelVersion": 1},
        )
        result = self.event(alias, event.id)
        self._verify(result, draft)
        return result
