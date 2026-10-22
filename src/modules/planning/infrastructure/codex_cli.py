import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from pydantic import ValidationError

from modules.planning.application.dto.planning import Plan, PlanningRequest
from modules.planning.application.ports.planner import Planner
from modules.planning.domain.skills import SkillBook
from shared.errors import AgentError

INSTRUCTIONS = """あなたは個人用Slackアシスタントの操作案生成器です。
JSON Schemaに適合する操作案を1つだけ返します。
"""

# 参照データに含まれる命令から、CLIのツールや接続済みアプリへ到達させない。
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "apps",
    "plugins",
    "hooks",
    "multi_agent",
    "multi_agent_v2",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "image_generation",
    "view_image",
    "memories",
    "skill_search",
    "goals",
    "chronicle",
    "workspace_dependencies",
)


def child_environment() -> dict[str, str]:
    # OpenAI APIキー、Slack/Notion/Googleの秘密情報、親CodexのIPC変数を継承しない。
    allowed = {"HOME", "PATH", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def strict_schema(schema: dict) -> dict:
    def visit(value):
        if isinstance(value, list):
            return [visit(item) for item in value]
        if not isinstance(value, dict):
            return value
        result = {
            key: visit(item)
            for key, item in value.items()
            if key not in {"default", "discriminator"}
        }
        if "oneOf" in result:
            result["anyOf"] = result.pop("oneOf")
        if result.get("type") == "object":
            result["additionalProperties"] = False
            result["required"] = list(result.get("properties", {}))
        return result

    return visit(schema)


def planning_input(request: PlanningRequest) -> str:
    data = request.model_dump(mode="json")
    # 永続化・既存の契約ではJSON文字列だが、モデルには入れ子の構造として渡す。
    # 旧データやメッセンジャーのプレーンテキストも参照データのまま保持する。
    for name in ("context", "thread"):
        try:
            data[name] = json.loads(data[name])
        except ValueError:
            pass
    return "入力JSON:\n" + json.dumps(data, ensure_ascii=False)


class CodexCLIPlanner(Planner):
    MODEL = "gpt-6-luna"
    REASONING_EFFORT = "medium"

    def __init__(
        self, skillbook: SkillBook, timeout_seconds: int = 120, executable: str | None = None
    ):
        self.instructions = INSTRUCTIONS + "\n" + skillbook.prompt()
        self.web_search = skillbook.policy.web_search
        self.executable = executable or shutil.which("codex") or "codex"
        self.timeout_seconds = timeout_seconds
        self.stopped = False
        # 評価・診断用。直近の公開Web操作のメタデータをメモリ内だけに保持する。
        self.last_web_searches: list[dict] = []

    def check_auth(self) -> None:
        try:
            result = subprocess.run(
                [self.executable, "login", "status"],
                env=child_environment(),
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            raise AgentError("codex_missing", "Codex CLIを起動できません。") from None
        status = result.stdout + result.stderr
        if result.returncode != 0 or "Logged in using ChatGPT" not in status:
            raise AgentError("codex_auth", "Codex CLIにChatGPTアカウントでログインしてください。")

    def _command(self, schema: Path, output: Path) -> list[str]:
        args = [
            self.executable,
            "exec",
            "--model",
            self.MODEL,
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--json",
            "--color",
            "never",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(output),
        ]
        overrides = {
            "model_provider": '"openai"',
            "model_reasoning_effort": json.dumps(self.REASONING_EFFORT),
            "forced_login_method": '"chatgpt"',
            "approval_policy": '"never"',
            "web_search": json.dumps(self.web_search),
            "mcp_servers": "{}",
            "features.skip_host_skill_discovery": "true",
            "developer_instructions": json.dumps(self.instructions, ensure_ascii=False),
        }
        for key, value in overrides.items():
            args.extend(["-c", f"{key}={value}"])
        for feature in DISABLED_FEATURES:
            args.extend(["--disable", feature])
        # 現行CLIは組込みWebツールの公開にもこのホストを使う。
        # シェル・アプリ・MCPなどの操作は上の設定で個別に無効化する。
        args.extend(["--enable" if self.web_search == "live" else "--disable", "code_mode_host"])
        args.append("-")
        return args

    def plan(self, request: PlanningRequest) -> Plan:
        self.last_web_searches = []
        if self.stopped:
            raise AgentError(
                "ai_stopped", "AIは認証・利用枠エラーで停止中です。確認後に再起動してください。"
            )
        self.check_auth()
        with tempfile.TemporaryDirectory(prefix="slack-agent-ai-") as directory:
            root = Path(directory)
            schema, output = root / "schema.json", root / "output.json"
            schema.write_text(json.dumps(strict_schema(Plan.model_json_schema())), encoding="utf-8")
            try:
                with subprocess.Popen(
                    self._command(schema, output),
                    cwd=root,
                    env=child_environment(),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                ) as process:
                    try:
                        stdout, stderr = process.communicate(
                            planning_input(request),
                            timeout=self.timeout_seconds,
                        )
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.communicate()
                        raise AgentError("ai_timeout", "AIの応答が時間切れになりました。") from None
                    if process.returncode:
                        # 生のstderrには認証情報や本文が含まれ得るため公開・ログ保存しない。
                        self.stopped = True
                        combined = (stdout + stderr).lower()
                        code = (
                            "ai_limit"
                            if any(
                                word in combined
                                for word in ("usage limit", "rate limit", "quota", "credits")
                            )
                            else "ai_unavailable"
                        )
                        raise AgentError(
                            code, "AIを停止しました。Codexの認証・利用枠を確認してください。"
                        )
            except OSError:
                raise AgentError("codex_missing", "Codex CLIを起動できません。") from None
            try:
                for line in stdout.splitlines():
                    event = json.loads(line)
                    item = event.get("item", {})
                    if event.get("type") in {"error", "turn.failed"}:
                        raise ValueError("failed turn")
                    if item.get("type") in {
                        "command_execution",
                        "mcp_tool_call",
                        "file_change",
                    }:
                        raise ValueError("unexpected tool call")
                    if item.get("type") == "web_search" and self.web_search == "disabled":
                        raise ValueError("web search is disabled")
                    if event.get("type") == "item.completed" and item.get("type") == "web_search":
                        self.last_web_searches.append(item.get("action", {}))
                return Plan.model_validate_json(output.read_text(encoding="utf-8"))
            except OSError, ValueError, ValidationError:
                raise AgentError(
                    "ai_invalid_output", "AIの操作案を検証できませんでした。"
                ) from None
