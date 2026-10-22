from modules.scheduling.domain.models import EventDraft, TimeRange


def draft() -> EventDraft:
    return EventDraft(
        title="面談",
        period=TimeRange.model_validate(
            {
                "start": "2026-10-23T18:30:00+09:00",
                "end": "2026-10-23T19:00:00+09:00",
            }
        ),
    )


def period() -> TimeRange:
    return TimeRange.model_validate(
        {"start": "2026-10-23T09:00:00+09:00", "end": "2026-10-23T18:00:00+09:00"}
    )
