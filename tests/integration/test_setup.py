import json
import os
import stat
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, create_autospec

import pytest
from slack_sdk import WebClient

from config import Settings
from diagnostics import (
    _slack,
    _socket,
    authorize_google,
    configuration_report,
    initialize,
    service_report,
)
from modules.scheduling.public import CalendarConfig
from shared.errors import AgentError


@pytest.fixture(autouse=True)
def clean_settings_environment(monkeypatch):
    for field in Settings.model_fields:
        monkeypatch.delenv(field.upper(), raising=False)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        team_id="T123",
        user_ids=frozenset({"U123"}),
        channel_ids=frozenset({"D123"}),
        google_token_file=tmp_path / "token.json",
        google_client_file=tmp_path / "client.json",
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
    )


def test_initialize_keeps_existing_secrets_and_uses_private_permissions(tmp_path: Path):
    (tmp_path / ".env.example").write_text("NOTION_TOKEN=\n")
    assert initialize(tmp_path) == [".env"]
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    assert not (tmp_path / "config").exists()
    (tmp_path / ".env").write_text("NOTION_TOKEN=secret-to-keep\n")
    assert initialize(tmp_path) == []
    assert (tmp_path / ".env").read_text() == "NOTION_TOKEN=secret-to-keep\n"


def test_missing_configuration_has_actionable_result(tmp_path: Path):
    config, checks = configuration_report(tmp_path / "missing.env")
    assert config is None and not checks[0].ok
    assert "task init" in checks[0].message


def test_configuration_error_does_not_print_invalid_secret(tmp_path: Path):
    path = tmp_path / "bad.env"
    path.write_text("TEAM_ID=private-secret-invalid\n")
    config, checks = configuration_report(path)
    assert config is None and not checks[0].ok
    assert "private-secret-invalid" not in checks[0].model_dump_json()


def test_configuration_report_rejects_example_ids_and_empty_tokens(tmp_path: Path, monkeypatch):
    example = Path(".env.example").resolve()
    monkeypatch.chdir(tmp_path)
    _, checks = configuration_report(example)
    assert len(checks) == 7
    assert all(not check.ok for check in checks)


def test_settings_loads_json_and_scalars_without_expanding_or_exporting_secrets(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text(
        "TEAM_ID=T123\nUSER_IDS='[\"U123\"]'\nCHANNEL_IDS='[\"D123\"]'\n"
        "NOTION_PAGES='[\"page\"]'\nNOTION_DATA_SOURCES='[\"source\"]'\n"
        "NOTION_COMPETITION_DATA_SOURCE_ID=3fe03e58e4948077a2d4000b2dc515fd\n"
        'NOTION_PARENTS=\'{"notes":"parent"}\'\n'
        'CALENDARS=\'{"personal":{"id":"calendar","writable":true,'
        '"member_data_source_id":"00000000-0000-0000-0000-000000000002"}}\'\n'
        "DATABASE=var/test.sqlite3\nRETENTION_DAYS=10\n"
        "SLACK_BOT_TOKEN='literal-${TEAM_ID}-$value-#token'\n"
    )
    config = Settings.load(path)
    assert config.notion_pages == frozenset({"page"})
    assert config.notion_data_sources == frozenset({"source"})
    competition = "3fe03e58-e494-8077-a2d4-000b2dc515fd"
    assert config.notion_competition_data_source_id == competition
    assert config.allowed_notion_data_sources == frozenset({"source", competition})
    assert config.agent_policy().notion_competition_data_source_id == competition
    assert config.notion_parents == {"notes": "parent"}
    assert config.calendars["personal"].writable
    assert config.calendars["personal"].member_data_source_id is not None
    assert config.database == Path("var/test.sqlite3")
    assert config.retention_days == 10
    assert config.slack_bot_token.get_secret_value() == "literal-${TEAM_ID}-$value-#token"
    assert "SLACK_BOT_TOKEN" not in os.environ


def test_environment_overrides_dotenv_for_all_setting_types(tmp_path: Path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "TEAM_ID=T_FILE\nUSER_IDS='[\"U123\"]'\nCHANNEL_IDS='[\"D123\"]'\n"
        "CALENDARS='{}'\nRETENTION_DAYS=7\nSLACK_BOT_TOKEN=file-token\n"
    )
    for key, value in {
        "TEAM_ID": "T_ENV",
        "USER_IDS": '["U456"]',
        "CALENDARS": '{"shared":{"id":"shared-calendar","writable":true}}',
        "RETENTION_DAYS": "10",
        "SLACK_BOT_TOKEN": "env-token",
    }.items():
        monkeypatch.setenv(key, value)
    config = Settings.load(path)
    assert config.team_id == "T_ENV"
    assert config.user_ids == frozenset({"U456"})
    assert config.calendars["shared"].writable
    assert config.retention_days == 10
    assert config.slack_bot_token.get_secret_value() == "env-token"


def test_environment_only_configuration_works_without_dotenv(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAM_ID", "T123")
    monkeypatch.setenv("USER_IDS", '["U123"]')
    monkeypatch.setenv("CHANNEL_IDS", '["D123"]')
    config, checks = configuration_report(tmp_path / "missing.env")
    assert config is not None and checks[0].ok


@pytest.mark.parametrize("value", ["", "invalid-source"])
def test_competition_source_is_optional_but_rejects_invalid_ids(tmp_path, monkeypatch, value):
    monkeypatch.setenv("NOTION_COMPETITION_DATA_SOURCE_ID", value)
    path = tmp_path / ".env"
    path.write_text("TEAM_ID=T123\nUSER_IDS='[\"U123\"]'\nCHANNEL_IDS='[\"D123\"]'\n")
    if value:
        with pytest.raises(ValueError):
            Settings.load(path)
    else:
        config = Settings.load(path)
        assert config.notion_competition_data_source_id is None
        assert config.allowed_notion_data_sources == frozenset()


@pytest.mark.parametrize(
    ("key", "value"),
    [("USER_IDS", "not-json-private"), ("CALENDARS", "[]"), ("MAX_READ_STEPS", "0")],
)
def test_invalid_environment_configuration_is_reported_without_values(
    tmp_path: Path, monkeypatch, key: str, value: str
):
    monkeypatch.setenv("TEAM_ID", "T123")
    monkeypatch.setenv("USER_IDS", '["U123"]')
    monkeypatch.setenv("CHANNEL_IDS", '["D123"]')
    monkeypatch.setenv(key, value)
    config, checks = configuration_report(tmp_path / "missing.env")
    assert config is None and not checks[0].ok
    assert "not-json-private" not in checks[0].model_dump_json()


@pytest.mark.parametrize("custom_path", [False, True])
def test_cli_loads_dotenv_without_task(tmp_path: Path, monkeypatch, custom_path):
    from apps.cli.main import main

    path = tmp_path / ("custom.env" if custom_path else ".env")
    path.write_text("TEAM_ID=T123\nUSER_IDS='[\"U123\"]'\nCHANNEL_IDS='[\"D123\"]'\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["kaggle-agent", *(["--config", str(path)] if custom_path else []), "run"]
    )
    run = Mock()
    monkeypatch.setattr("apps.cli.main.run", run)
    main()
    assert run.call_args.args[0].team_id == "T123"


def test_socket_check_uses_app_token_without_returning_ticket(tmp_path: Path, monkeypatch):
    client = create_autospec(WebClient, instance=True)
    client.apps_connections_open.return_value = {"url": "wss://slack.test/?ticket=secret"}
    monkeypatch.setattr("diagnostics.WebClient", lambda **kwargs: client)
    assert _socket(settings(tmp_path)) is None
    client.apps_connections_open.assert_called_once_with(app_token="xapp-test")
    client.chat_postMessage.assert_not_called()


@pytest.mark.parametrize(
    "channel",
    [
        {"is_im": True, "user": "U_OTHER"},
        {"is_im": False, "is_member": False},
        {"is_im": False, "is_member": True, "is_archived": True},
    ],
)
def test_slack_diagnostics_reject_wrong_dm_or_missing_membership(tmp_path, monkeypatch, channel):
    client = create_autospec(WebClient, instance=True)
    client.auth_test.return_value = {"team_id": "T123"}
    client.conversations_info.return_value = {"channel": channel}
    monkeypatch.setattr("diagnostics.WebClient", lambda **kwargs: client)
    with pytest.raises(AgentError):
        _slack(settings(tmp_path))
    client.chat_postMessage.assert_not_called()


def test_provider_diagnostics_redact_errors_and_continue(tmp_path: Path, monkeypatch):
    for name in ("_slack", "_socket", "_notion", "_calendar"):
        monkeypatch.setattr(
            "diagnostics." + name,
            Mock(side_effect=RuntimeError("private-provider-error")),
        )
    checks = service_report(settings(tmp_path))
    assert len(checks) == 4 and all(not check.ok for check in checks)
    assert "private-provider-error" not in json.dumps([check.model_dump() for check in checks])


@pytest.mark.parametrize("read_only", [False, True])
def test_google_auth_saves_private_token_only_after_success(tmp_path: Path, monkeypatch, read_only):
    config = settings(tmp_path).model_copy(
        update={"calendars": {"test": CalendarConfig(id="test", writable=not read_only)}}
    )
    config.google_client_file.write_text(json.dumps({"installed": {"client_id": "test"}}))
    flow = Mock()
    flow.run_local_server.return_value.to_json.return_value = '{"token":"private-token"}'
    factory = Mock(return_value=flow)
    monkeypatch.setattr("diagnostics.InstalledAppFlow.from_client_config", factory)
    authorize_google(config)
    assert config.google_token_file.read_text() == '{"token":"private-token"}'
    assert stat.S_IMODE(config.google_token_file.stat().st_mode) == 0o600
    assert factory.call_args.kwargs["autogenerate_code_verifier"] is True
    scopes = factory.call_args.kwargs["scopes"]
    assert "https://www.googleapis.com/auth/calendar.events.owned" in scopes
    assert "https://www.googleapis.com/auth/calendar.calendars.readonly" in scopes
    assert "https://www.googleapis.com/auth/calendar.events" not in scopes
    assert ("https://www.googleapis.com/auth/calendar.events.readonly" in scopes) is read_only
    assert flow.run_local_server.call_args.kwargs["host"] == "127.0.0.1"


def test_google_auth_failure_keeps_existing_token(tmp_path: Path, monkeypatch):
    config = settings(tmp_path)
    config.google_client_file.write_text(json.dumps({"installed": {"client_id": "test"}}))
    config.google_token_file.write_text("previous-token")
    flow = Mock()
    flow.run_local_server.side_effect = TimeoutError()
    monkeypatch.setattr("diagnostics.InstalledAppFlow.from_client_config", Mock(return_value=flow))
    with pytest.raises(TimeoutError):
        authorize_google(config)
    assert config.google_token_file.read_text() == "previous-token"


def test_google_auth_rejects_web_client_before_starting_browser(tmp_path: Path):
    config = settings(tmp_path)
    config.google_client_file.write_text('{"web":{}}')
    with pytest.raises(AgentError, match="デスクトップ"):
        authorize_google(config)


def test_slack_connection_failure_does_not_start_worker(tmp_path: Path, monkeypatch):
    from apps.slack.main import run

    config = settings(tmp_path).model_copy(update={"database": tmp_path / "agent.sqlite3"})
    agent = Mock()
    monkeypatch.setattr("apps.slack.main.build", lambda *_: (Mock(), agent))
    handler = Mock()
    handler.connect.side_effect = AgentError("socket", "接続できません")
    monkeypatch.setattr("apps.slack.main.SocketModeHandler", lambda *_: handler)
    thread = Mock()
    thread.ident = None
    monkeypatch.setattr("apps.slack.main.threading.Thread", lambda **kwargs: thread)
    with pytest.raises(AgentError):
        run(config)
    thread.start.assert_not_called()
    handler.close.assert_called_once()


def test_calendar_diagnostics_rejects_write_target_without_ownership(tmp_path: Path, monkeypatch):
    from diagnostics import _calendar
    from modules.scheduling.public import CalendarConfig

    config = settings(tmp_path).model_copy(
        update={
            "calendars": {"shared": CalendarConfig(id="test-calendar", writable=True)},
        }
    )
    monkeypatch.setattr("diagnostics.Credentials.from_authorized_user_file", Mock())
    api = Mock()
    api.request.return_value = {"accessRole": "writer"}
    monkeypatch.setattr("diagnostics.JsonHttp", lambda _: api)
    with ExitStack() as stack, pytest.raises(AgentError, match="所有"):
        _calendar(config, stack)


def test_application_policy_excludes_runtime_secrets(tmp_path):
    config = settings(tmp_path)
    policy = config.agent_policy()
    assert policy.team_id == config.team_id
    assert policy.user_ids == config.user_ids
    assert (
        not {"slack_bot_token", "slack_app_token", "notion_token", "database", "google_token_file"}
        & policy.model_dump().keys()
    )


def test_daily_reminder_json_loads_and_stays_outside_ai_policy(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "TEAM_ID=T1\nUSER_IDS='[\"U1\"]'\nCHANNEL_IDS='[\"C1\"]'\n"
        'CALENDARS=\'{"club":{"id":"calendar"}}\'\n'
        'DAILY_SCHEDULE_REMINDER=\'{"calendar_alias":"club","label_id":"team",'
        '"channel_id":"C1"}\'\n'
    )
    config = Settings.load(path)
    assert config.daily_schedule_reminder is not None
    assert config.daily_schedule_reminder.hour == 10
    assert config.daily_schedule_reminder.minute == 0
    assert "daily_schedule_reminder" not in config.agent_policy().model_dump()


@pytest.mark.parametrize(
    "override",
    [
        {"calendar_alias": "unknown"},
        {"channel_id": "COTHER"},
        {"channel_id": "D1"},
        {"hour": 24},
        {"minute": 60},
        {"label_id": ""},
    ],
)
def test_daily_reminder_rejects_unallowed_targets_and_invalid_schedule(tmp_path, override):
    with pytest.raises(ValueError):
        Settings.model_validate(
            {
                "team_id": "T1",
                "user_ids": ["U1"],
                "channel_ids": ["C1"],
                "calendars": {"club": {"id": "calendar"}},
                "daily_schedule_reminder": {
                    "calendar_alias": "club",
                    "label_id": "team",
                    "channel_id": "C1",
                    **override,
                },
            }
        )


def test_runtime_starts_reminders_in_a_separate_thread(tmp_path, monkeypatch):
    from apps.slack.main import run
    from apps.worker.reminders import run_reminders
    from modules.interaction.public import DailyScheduleReminder

    config = settings(tmp_path).model_copy(
        update={
            "database": tmp_path / "agent.sqlite3",
            "daily_schedule_reminder": DailyScheduleReminder(
                calendar_alias="club", label_id="team", channel_id="C1"
            ),
        }
    )
    monkeypatch.setattr("apps.slack.main.build", lambda *_: (Mock(), Mock()))
    workers = []

    def create_thread(**kwargs):
        thread = Mock(ident=1)
        workers.append((kwargs, thread))
        return thread

    monkeypatch.setattr("apps.slack.main.threading.Thread", create_thread)
    handlers = {}
    monkeypatch.setattr(
        "apps.slack.main.signal.signal", lambda sig, handler: handlers.setdefault(sig, handler)
    )
    handler = Mock()
    # 停止を要求してmainの待機を解除する。実際のスレッドや接続は作らない。
    handler.connect.side_effect = lambda: next(iter(handlers.values()))(None, None)
    monkeypatch.setattr("apps.slack.main.SocketModeHandler", lambda *_: handler)
    run(config)
    assert len(workers) == 2
    assert workers[1][0]["target"] is run_reminders
    for _, thread in workers:
        thread.start.assert_called_once()
        thread.join.assert_called_once()
