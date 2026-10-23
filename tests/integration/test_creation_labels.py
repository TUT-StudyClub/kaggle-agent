import pytest
from pydantic import ValidationError

from modules.interaction.domain.models import JobStatus
from modules.planning.public import Plan
from modules.scheduling.domain.models import EventLabel
from modules.scheduling.public import CalendarConfig, CreateEvent, UpdateEvent
from tests.support import draft

LABEL = EventLabel(id="kaggle", name="Kaggle", background_color="#7cb342")
OTHER = EventLabel(id="unnamed", background_color="#7cb342")


@pytest.fixture
def labelled(harness, monkeypatch):
    labels = [OTHER, LABEL]
    calendar = harness.calendar.calendars()[0]
    monkeypatch.setattr(
        harness.calendar,
        "calendars",
        lambda: [calendar.model_copy(update={"labels": list(labels)})],
    )
    config = harness.policy.calendars["personal"].model_copy(update={"creation_label": LABEL})
    harness.configure(policy=harness.policy.model_copy(update={"calendars": {"personal": config}}))
    harness.planner.steps = [Plan(action=CreateEvent(calendar_alias="personal", draft=draft()))]
    return harness, labels


def test_creation_snapshots_configured_label_instead_of_first_same_color(labelled):
    harness, _ = labelled
    job_id = harness.submit()
    harness.agent.run_once()
    waiting = harness.jobs.get(job_id)
    assert waiting.status == JobStatus.AWAITING_APPROVAL and harness.calendar.writes == 0
    action = Plan.model_validate_json(waiting.plan_json).action
    assert isinstance(action, CreateEvent) and action.draft.label == LABEL
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert harness.calendar.stored[job_id].label == LABEL
    harness.approve(job_id)
    assert not harness.agent.run_once()
    assert harness.calendar.writes == 1


@pytest.mark.parametrize("change", ["missing", "name", "color"])
def test_missing_or_changed_label_stops_after_approval_without_using_same_color(labelled, change):
    harness, labels = labelled
    job_id = harness.submit()
    harness.agent.run_once()
    labels.pop()
    if change != "missing":
        field = "name" if change == "name" else "background_color"
        labels.append(LABEL.model_copy(update={field: "other" if change == "name" else "#039be5"}))
    harness.approve(job_id)
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "creation_label_changed"
    assert harness.calendar.writes == 0


def test_label_id_is_required_even_when_name_and_color_match(labelled):
    harness, labels = labelled
    labels[-1] = LABEL.model_copy(update={"id": "different"})
    job_id = harness.submit()
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "creation_label_changed"
    assert harness.calendar.writes == 0


def test_ai_cannot_replace_configured_label_with_another_same_color(labelled):
    harness, _ = labelled
    harness.planner.steps = [
        Plan(
            action=CreateEvent(
                calendar_alias="personal", draft=draft().model_copy(update={"label": OTHER})
            )
        )
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "forbidden_event_label"
    assert harness.calendar.writes == 0


def test_without_configuration_ai_cannot_invent_a_creation_label(harness):
    harness.planner.steps = [
        Plan(
            action=CreateEvent(
                calendar_alias="personal", draft=draft().model_copy(update={"label": LABEL})
            )
        )
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    assert harness.jobs.get(job_id).error_code == "forbidden_event_label"
    assert harness.calendar.writes == 0


@pytest.mark.parametrize(
    "label",
    [LABEL.model_copy(update={"id": ""}), LABEL.model_copy(update={"background_color": "green"})],
)
def test_creation_label_configuration_requires_id_and_exact_hex_color(label):
    with pytest.raises(ValidationError):
        CalendarConfig(id="calendar", writable=True, creation_label=label)


def test_ordinary_update_cannot_change_existing_label():
    with pytest.raises(ValidationError):
        UpdateEvent(
            calendar_alias="personal",
            event_id="existing",
            draft=draft().model_copy(update={"label": LABEL}),
        )
