import json
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import httpx
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from pydantic import ValidationError
from slack_sdk import WebClient
from slack_sdk.errors import SlackClientError

from config import Settings
from shared.domain.value_object import ValueObject
from shared.errors import AgentError
from shared.infrastructure.google_auth import GoogleAuth
from shared.infrastructure.http import JsonHttp


class Diagnostic(ValueObject):
    name: str
    ok: bool
    message: str


def authorize_google(settings: Settings) -> None:
    """Google公式のローカルサーバー認証を使い、トークンを所有者限定で保存する。"""
    try:
        raw = json.loads(settings.google_client_file.read_text(encoding="utf-8"))
    except OSError, ValueError:
        raise AgentError(
            "google_client", "デスクトップアプリ用OAuthクライアントのJSONを設定してください。"
        ) from None
    if not isinstance(raw, dict) or "installed" not in raw:
        raise AgentError(
            "google_client", "Google OAuthクライアントはデスクトップアプリ用を選択してください。"
        )
    scopes = [
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.calendars.readonly",
        "https://www.googleapis.com/auth/calendar.events.owned",
        "https://www.googleapis.com/auth/calendar.freebusy",
    ]
    if any(not calendar.writable for calendar in settings.calendars.values()):
        scopes.append("https://www.googleapis.com/auth/calendar.events.readonly")
    flow = InstalledAppFlow.from_client_config(raw, scopes=scopes, autogenerate_code_verifier=True)
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        open_browser=True,
        timeout_seconds=180,
        authorization_prompt_message="ブラウザでGoogleアカウントの認証を完了してください。",
        success_message="認証が完了しました。このタブを閉じてください。",
        access_type="offline",
        prompt="consent",
    )
    output = settings.google_token_file
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    pending: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False, prefix=".google-token-"
        ) as stream:
            pending = Path(stream.name)
            stream.write(credentials.to_json())
        os.replace(pending, output)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def initialize(project: Path) -> list[str]:
    """ローカル設定を所有者限定で作成する。再実行しても既存ファイルを上書きしない。"""
    content = (project / ".env.example").read_bytes()
    path = project / ".env"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return []
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    return [".env"]


def configuration_report(path: Path) -> tuple[Settings | None, list[Diagnostic]]:
    try:
        settings = Settings.load(path)
    except OSError, ValueError, ValidationError:
        return None, [
            Diagnostic(
                name="設定ファイル",
                ok=False,
                message=(
                    "task initで.envを作成し、接続先IDとトークンを設定してください。"
                    "配列・オブジェクトはJSON形式です。値は表示しません。"
                ),
            )
        ]
    ids_valid = (
        re.fullmatch(r"T[A-Z0-9]+", settings.team_id) is not None
        and all(re.fullmatch(r"[UW][A-Z0-9]+", value) for value in settings.user_ids)
        and all(re.fullmatch(r"[CDG][A-Z0-9]+", value) for value in settings.channel_ids)
    )
    try:
        ids = [
            *settings.notion_pages,
            *settings.notion_parents.values(),
            *settings.allowed_notion_data_sources,
        ]
        notion_valid = bool(ids) and all(UUID(value).int > 1 for value in ids)
    except ValueError:
        notion_valid = False
    calendars_valid = bool(settings.calendars) and all(
        "REPLACE" not in value.id for value in settings.calendars.values()
    )
    checks = [
        ("Slack接続先ID", ids_valid, ".envに実際のSlack IDを設定してください。"),
        ("Notion接続先ID", notion_valid, "Notionの許可ページまたは親ページIDを設定してください。"),
        ("Calendar接続先ID", calendars_valid, "カレンダーの参照先・作成先IDを設定してください。"),
        (
            "SLACK_BOT_TOKEN",
            settings.slack_bot_token.get_secret_value().startswith("xoxb-"),
            ".envにBot User OAuth Tokenを設定してください。",
        ),
        (
            "SLACK_APP_TOKEN",
            settings.slack_app_token.get_secret_value().startswith("xapp-"),
            ".envにconnections:writeを持つApp-Level Tokenを設定してください。",
        ),
        (
            "NOTION_TOKEN",
            bool(settings.notion_token.get_secret_value()),
            ".envにNotionのインテグレーションシークレットを設定してください。",
        ),
        (
            "Google OAuth",
            settings.google_token_file.is_file(),
            "設定したGOOGLE_TOKEN_FILEの場所にauthorized-user形式の認証ファイルを置いてください。",
        ),
    ]
    return settings, [
        Diagnostic(name=name, ok=bool(ok), message="設定あり" if ok else message)
        for name, ok, message in checks
    ]


def service_report(settings: Settings) -> list[Diagnostic]:
    """接続先のメタデータのみ検査。Slack投稿・Notion/Calendar書込み・AI推論は行わない。"""
    results = []
    with ExitStack() as stack:
        for name, check in (
            ("Slack Botと許可チャンネル", lambda: _slack(settings)),
            ("Socket Mode", lambda: _socket(settings)),
            ("Notion接続先", lambda: _notion(settings, stack)),
            ("Google Calendar接続先", lambda: _calendar(settings, stack)),
        ):
            try:
                check()
            except AgentError as error:
                results.append(Diagnostic(name=name, ok=False, message=error.message))
            except Exception:
                results.append(
                    Diagnostic(
                        name=name, ok=False, message="認証・権限・ネットワークを確認してください。"
                    )
                )
            else:
                results.append(Diagnostic(name=name, ok=True, message="接続確認済み"))
    return results


def _slack(settings: Settings) -> None:
    client = WebClient(token=settings.slack_bot_token.get_secret_value(), retry_handlers=[])
    auth = client.auth_test()
    if auth["team_id"] != settings.team_id:
        raise AgentError("wrong_workspace", "Slack Botのワークスペースが設定と一致しません。")
    if settings.daily_schedule_reminder is not None or settings.daily_task_reminder is not None:
        try:
            client.users_info(user=auth["user_id"])
        except SlackClientError, OSError:
            raise AgentError(
                "slack_members_permission",
                "定期通知にはusers:readが必要です。権限を追加してAppを再インストールしてください。",
            ) from None
    for channel in sorted(settings.channel_ids):
        raw = client.conversations_info(channel=channel)["channel"]
        if raw.get("is_archived") or (not raw.get("is_im") and not raw.get("is_member")):
            raise AgentError("slack_membership", "Botを許可チャンネルへ招待してください。")
        if raw.get("is_im") and raw.get("user") not in settings.user_ids:
            raise AgentError("wrong_dm", "DMの相手が許可ユーザーと一致しません。")


def _socket(settings: Settings) -> None:
    # 返される一時WebSocket URLにはチケットが含まれるため出力・保存しない。
    client = WebClient(token=settings.slack_app_token.get_secret_value(), retry_handlers=[])
    if not client.apps_connections_open(app_token=settings.slack_app_token.get_secret_value()).get(
        "url"
    ):
        raise AgentError("socket_mode", "Socket Modeの接続URLを取得できません。")


def _notion(settings: Settings, stack: ExitStack) -> None:
    client = stack.enter_context(
        httpx.Client(
            base_url="https://api.notion.com/v1/",
            timeout=15,
            headers={
                "Authorization": f"Bearer {settings.notion_token.get_secret_value()}",
                "Notion-Version": "2025-09-03",
            },
        )
    )
    api = JsonHttp(client)
    api.request("GET", "users/me")
    for page_id in sorted(set(settings.notion_pages) | set(settings.notion_parents.values())):
        raw = api.request("GET", f"pages/{UUID(page_id)}")
        if raw.get("in_trash") or raw.get("archived"):
            raise AgentError("notion_archived", "許可ページがアーカイブされています。")
    member_sources = {
        c.member_data_source_id for c in settings.calendars.values() if c.member_data_source_id
    }
    for source_id in sorted(set(settings.allowed_notion_data_sources) | member_sources):
        api.request("GET", f"data_sources/{UUID(source_id)}")


def _calendar(settings: Settings, stack: ExitStack) -> None:
    credentials = Credentials.from_authorized_user_file(str(settings.google_token_file))
    client = stack.enter_context(
        httpx.Client(
            base_url="https://www.googleapis.com/calendar/v3/",
            timeout=15,
            auth=GoogleAuth(credentials),
        )
    )
    api = JsonHttp(client)
    for calendar in settings.calendars.values():
        raw = api.request("GET", f"users/me/calendarList/{quote(calendar.id, safe='')}")
        if calendar.writable and raw.get("accessRole") != "owner":
            raise AgentError(
                "calendar_owner", "書き込み先には自分が所有するカレンダーを指定してください。"
            )
        api.request("GET", f"calendars/{quote(calendar.id, safe='')}")
