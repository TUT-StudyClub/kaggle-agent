import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from modules.kaggle.application.ports.runner import KaggleRunner
from modules.kaggle.domain.commands import KaggleInvocation
from shared.errors import AgentError, UncertainWrite


class KaggleCLIRunner(KaggleRunner):
    """検証済みのCLI引数だけをシェルを介さず実行する。カタログはKaggle 2.2.5に基づく。"""

    def __init__(
        self,
        workspace: Path,
        config_dir: Path,
        token: str = "",
        timeout_seconds: int = 120,
    ):
        self.workspace = workspace.resolve()
        self.config_dir = config_dir.expanduser().resolve()
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.catalog = json.loads(Path(__file__).with_name("commands.json").read_text())

    def _spec(self, command: str) -> dict:
        if command not in self.catalog:
            raise AgentError(
                "kaggle_command", "未対応のKaggleコマンドだよ。kaggle.helpで確認してね。"
            )
        return self.catalog[command]

    def describe(self, command: str, may_write: bool) -> dict[str, object]:
        if not command:
            return {
                "kaggle_commands": {
                    name: "write" if spec["write"] else "read"
                    for name, spec in self.catalog.items()
                    if may_write or not spec["write"]
                },
                "kaggle_may_write": may_write,
                "workspace_paths": "作成・アップロード元はKAGGLE_WORKSPACEからの相対パス。",
                "downloads": "取得先はPythonが操作ごとに分離する。--pathは指定しない。",
            }
        spec = self._spec(command)
        return {"kaggle_command": command, **spec, "kaggle_may_write": may_write}

    def prepare(self, command: str, arguments: list[str]) -> KaggleInvocation:
        spec = self._spec(command)
        flags = {flag: option for option in spec["options"] for flag in option["flags"]}
        positional = [option for option in spec["options"] if not option["flags"]]
        # 略記・結合した短いオプションはCLIに解釈させず、既知の完全なフラグだけを通す。
        tokens = []
        for arg in arguments:
            if not arg or len(arg) > 2048 or any(ord(char) < 32 for char in arg):
                raise AgentError("kaggle_arguments", "Kaggleの引数を確認してね。")
            if arg.startswith("--") and "=" in arg:
                tokens.extend(arg.split("=", 1))
            else:
                tokens.append(arg)
        pairs: list[tuple[dict, str | None]] = []
        position = 0
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("-"):
                option = flags.get(token)
                if option is None:
                    raise AgentError("kaggle_arguments", "未対応のKaggleオプションだよ。")
                value = None
                if option["nargs"] != 0:
                    if index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
                        index += 1
                        value = tokens[index]
                    elif option["nargs"] != "?":
                        raise AgentError("kaggle_arguments", "Kaggleオプションの値が必要だよ。")
                pairs.append(({**option, "used_flag": token}, value))
            else:
                if position >= len(positional):
                    raise AgentError("kaggle_arguments", "Kaggleの位置引数が多すぎるよ。")
                option = positional[position]
                pairs.append((option, token))
                if option["nargs"] != "+":
                    position += 1
            index += 1
        write = spec["write"] or (
            command == "datasets metadata" and any(opt["dest"] == "update" for opt, _ in pairs)
        )
        normalized = []
        for option, value in pairs:
            dest = option["dest"]
            if dest in {"unzip", "untar", "follow", "wait"}:
                raise AgentError(
                    "kaggle_arguments",
                    "展開・追従・完了待ちは使えないよ。状態確認を別に依頼してね。",
                )
            local_path = dest in {"path", "folder", "file_path", "local_paths", "file"} or (
                command == "competitions submit" and dest == "file_name"
            )
            if not write and (local_path or dest == "output"):
                raise AgentError("kaggle_path", "Kaggleの取得先はPythonが自動で指定するよ。")
            if local_path and value is not None:
                value = str(self._safe_path(value))
            if dest in {"file_name", "file_pattern"} and not local_path and value is not None:
                if (
                    "\\" in value
                    or PurePosixPath(value).is_absolute()
                    or ".." in PurePosixPath(value).parts
                ):
                    raise AgentError("kaggle_path", "取得するファイル名を確認してね。")
            if option["flags"]:
                normalized.append(option["used_flag"])
            if value is not None:
                normalized.append(value)
        return KaggleInvocation(command=command, arguments=tuple(normalized), write=write)

    def _safe_path(self, value: str) -> Path:
        path = Path(value)
        path = path if path.is_absolute() else self.workspace / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise AgentError("kaggle_path", "Kaggle作業ディレクトリの外は読み書きできないよ。")
        # アップロードするディレクトリ内のリンクからも、作業範囲外を読ませない。
        if resolved.is_dir():
            for child in resolved.rglob("*"):
                if child.is_symlink():
                    raise AgentError(
                        "kaggle_path", "アップロード元にはシンボリックリンクを含めないでね。"
                    )
        return resolved

    def run(self, invocation: KaggleInvocation, key: str) -> dict[str, object]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key):
            raise AgentError("kaggle_job", "Kaggle操作のジョブIDを確認できないよ。")
        # 引数は呼出しのたびに再検証する。承認待ちの間に変わったリンクも検査する。
        checked = self.prepare(invocation.command, list(invocation.arguments))
        if checked != invocation:
            raise AgentError("kaggle_arguments", "Kaggleの実行引数が変更されたよ。")
        if not self.token and not any(
            (self.config_dir / name).is_file()
            for name in ("access_token", "kaggle.json", "oauth.json")
        ):
            raise AgentError("kaggle_auth", "共有するKaggle認証を実行環境に設定してね。")
        cwd = self.workspace / ("owner" if invocation.write else f"downloads/{key}/{uuid4().hex}")
        if cwd.exists() and cwd.is_symlink():
            raise AgentError("kaggle_path", "Kaggle作業先にシンボリックリンクは使えないよ。")
        self._safe_path(str(cwd))
        cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        args = list(invocation.arguments)
        spec = self._spec(invocation.command)
        if not invocation.write:
            for option in spec["options"]:
                if option["dest"] in {"path", "folder", "output"} and option["nargs"] != 0:
                    args.extend([option["flags"][-1], str(cwd)])
        else:
            # CLIの対話確認は既存のSlack承認で代替する。
            if any(option["dest"] == "no_confirm" for option in spec["options"]):
                args.append("--yes")
            for name in ("kernel-metadata.json",):
                folders = [cwd]
                for index, arg in enumerate(args[:-1]):
                    if arg in {"--path", "-p"}:
                        folders.append(Path(args[index + 1]))
                for folder in folders:
                    metadata = folder / name
                    if invocation.command == "kernels push" and metadata.is_file():
                        raw = json.loads(metadata.read_text())
                        code = folder / str(raw.get("code_file", ""))
                        self._safe_path(str(code))
        env = {
            k: v for k, v in os.environ.items() if k in {"HOME", "PATH", "LANG", "LC_ALL", "TMPDIR"}
        }
        env["KAGGLE_CONFIG_DIR"] = str(self.config_dir)
        if self.token:
            env["KAGGLE_API_TOKEN"] = self.token
        command = [sys.executable, "-I", "-m", "kaggle", *invocation.command.split(), *args]
        with tempfile.TemporaryFile() as output:
            try:
                with subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=output,
                    start_new_session=True,
                ) as process:
                    try:
                        code = process.wait(timeout=self.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        if invocation.write:
                            raise UncertainWrite() from None
                        raise AgentError(
                            "kaggle_timeout", "Kaggleの応答待ちでタイムアウトしたよ。"
                        ) from None
            except OSError:
                raise AgentError("kaggle_cli", "Kaggle CLIを起動できなかったよ。") from None
            if code != 0:
                # CLI例外にはトークンやファイル内容を含みうるためそのまま返さない。
                raise AgentError(
                    "kaggle_failed", "Kaggle操作に失敗したよ。認証・権限・入力を確認してね。"
                )
            output.seek(0)
            raw_output = output.read(48001)
        text = raw_output[:48000].decode("utf-8", errors="replace")
        if self.token:
            text = text.replace(self.token, "[REDACTED]")
        files = [str(path.relative_to(self.workspace)) for path in cwd.rglob("*") if path.is_file()]
        return {
            "kaggle_command": invocation.command,
            "output": text,
            "output_truncated": len(raw_output) > 48000,
            "files": sorted(files)[:50],
        }
