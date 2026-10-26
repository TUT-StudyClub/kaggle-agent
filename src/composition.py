from contextlib import ExitStack

import httpx
from google.oauth2.credentials import Credentials
from slack_bolt import App
from slack_sdk import WebClient

from apps.slack.handlers.events import register_listeners
from config import Settings
from modules.interaction.composition import build_interaction
from modules.interaction.public import InteractionFacade
from modules.kaggle.composition import build_kaggle
from modules.knowledge.composition import build_knowledge, build_task_board
from modules.planning.composition import build_planning
from modules.scheduling.composition import build_scheduling
from shared.application.ports.clock import Clock
from shared.errors import AgentError
from shared.infrastructure.clock import SystemClock
from shared.infrastructure.google_auth import GoogleAuth
from shared.infrastructure.http import JsonHttp


def build_clock() -> Clock:
    return SystemClock()


def build(settings: Settings, stack: ExitStack) -> tuple[App, InteractionFacade]:
    if (
        not settings.slack_bot_token.get_secret_value()
        or not settings.slack_app_token.get_secret_value()
    ):
        raise AgentError("slack_configuration", "Slackトークンを環境変数に設定してください。")
    if not settings.notion_token.get_secret_value() or not settings.google_token_file.exists():
        raise AgentError(
            "service_configuration", "NotionとGoogle OAuthの認証情報を設定してください。"
        )
    clock = build_clock()
    planning = build_planning(settings.codex_timeout_seconds, clock=clock)
    slack_client = WebClient(token=settings.slack_bot_token.get_secret_value(), retry_handlers=[])
    auth = slack_client.auth_test()
    if auth["team_id"] != settings.team_id:
        raise AgentError("wrong_workspace", "Slackトークンのワークスペースが設定と一致しません。")
    notion_client = stack.enter_context(
        httpx.Client(
            base_url="https://api.notion.com/v1/",
            timeout=30,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {settings.notion_token.get_secret_value()}",
                "Notion-Version": "2025-09-03",
            },
        )
    )
    credentials = Credentials.from_authorized_user_file(str(settings.google_token_file))
    calendar_client = stack.enter_context(
        httpx.Client(
            base_url="https://www.googleapis.com/calendar/v3/",
            timeout=30,
            auth=GoogleAuth(credentials),
            follow_redirects=False,
        )
    )
    notion_http = JsonHttp(notion_client)
    knowledge = build_knowledge(
        notion_http,
        set(settings.notion_pages),
        settings.notion_parents,
        set(settings.allowed_notion_data_sources),
    )
    scheduling = build_scheduling(JsonHttp(calendar_client), notion_http, settings.calendars)
    kaggle = build_kaggle(
        settings.kaggle_workspace,
        settings.kaggle_config_dir,
        settings.kaggle_api_token.get_secret_value(),
        settings.kaggle_owner_user_id,
    )
    agent = build_interaction(
        settings.database,
        settings.agent_policy(),
        slack_client,
        planning,
        knowledge,
        scheduling,
        planning.skillbook,
        clock=clock,
        daily_schedule_reminder=settings.daily_schedule_reminder,
        daily_task_reminder=settings.daily_task_reminder,
        task_board=(
            build_task_board(notion_http, settings.daily_task_reminder)
            if settings.daily_task_reminder is not None
            else None
        ),
        bot_user_id=auth["user_id"],
        kaggle=kaggle,
    )
    app = App(client=slack_client, token_verification_enabled=False)
    register_listeners(app, agent, auth["user_id"])
    return app, agent
