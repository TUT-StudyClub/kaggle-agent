import fcntl
import hashlib
import os
import re
import tempfile
from pathlib import Path

import yaml

from modules.interaction.application.services.tools import PythonTool
from modules.interaction.domain.models import Source
from modules.planning.public import Action, ReadInstructions, UpdateInstructions
from shared.errors import AgentError


class InstructionTool(PythonTool):
    """読み取りと承認済みMarkdownの更新だけを扱う。"""

    def __init__(self, root: Path):
        self.root = root.resolve()

    @property
    def name(self) -> str:
        return "instructions"

    @property
    def actions(self) -> frozenset[str]:
        return frozenset({"instructions.read", "instructions.update"})

    def _path(self, name: str) -> Path:
        if not re.fullmatch(
            r"(?:rules/(?:[a-z0-9]+(?:-[a-z0-9]+)*)\.md|skills/(?:[a-z0-9]+(?:-[a-z0-9]+)*)/SKILL\.md)",
            name,
        ):
            raise AgentError("instruction_path", "変更できるのはスキルとルールのMarkdownだけだよ。")
        path = self.root / name
        if path.resolve() != path:
            raise AgentError("instruction_path", "シンボリックリンクは変更できないよ。")
        return path

    def _current(self, path: Path) -> str | None:
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def prepare(self, action: Action, source: Source) -> None:
        if not isinstance(action, UpdateInstructions):
            return
        path = self._path(action.path)
        current = self._current(path)
        digest = hashlib.sha256(current.encode()).hexdigest() if current is not None else "missing"
        if digest != action.previous_sha256:
            raise AgentError("instruction_conflict", "内容が変わったので、もう一度確認してね。")
        if not action.content.strip():
            raise AgentError("instruction_invalid", "空の内容には変更できないよ。")
        if action.path.startswith("skills/"):
            lines = action.content.splitlines(keepends=True)
            try:
                if not lines or lines[0].strip() != "---":
                    raise ValueError
                end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
                data = yaml.safe_load("".join(lines[1:end]))
                if (
                    not isinstance(data, dict)
                    or set(data) != {"name", "description"}
                    or data["name"] != path.parent.name
                    or len(path.parent.name) > 64
                    or not isinstance(data["description"], str)
                    or not data["description"].strip()
                    or not "".join(lines[end + 1 :]).strip()
                ):
                    raise ValueError
            except ValueError, StopIteration, yaml.YAMLError:
                raise AgentError("instruction_invalid", "スキルの形式を確認してね。") from None

    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict:
        if isinstance(action, ReadInstructions):
            paths = sorted((self.root / "rules").glob("*.md")) + sorted(
                (self.root / "skills").glob("*/SKILL.md")
            )
            files = []
            for path in paths:
                relative = str(path.relative_to(self.root))
                try:
                    path = self._path(relative)
                    content = self._current(path)
                except AgentError, OSError, UnicodeError:
                    continue
                if content is not None:
                    files.append(
                        {
                            "path": relative,
                            "content": content,
                            "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        }
                    )
            return {"files": files}
        if not isinstance(action, UpdateInstructions) or source is None:
            raise AgentError("instruction_invalid", "変更内容を確認できなかったよ。")
        lock_dir = self.root / "var"
        lock_dir.mkdir(exist_ok=True)
        with (lock_dir / "instruction-update.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.prepare(action, source)
            path = self._path(action.path)
            path.parent.mkdir(exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".instruction-")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    stream.write(action.content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp, path)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        return {"updated": action.path}

    def reconcile(self, action: Action, key: str) -> dict | None:
        return None
