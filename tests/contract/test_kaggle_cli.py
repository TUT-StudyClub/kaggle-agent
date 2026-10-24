import os
import subprocess

import pytest

from modules.kaggle.composition import compose_kaggle
from modules.kaggle.infrastructure.cli import KaggleCLIRunner
from modules.kaggle.public import ReadKaggle, WriteKaggle
from shared.errors import AgentError, UncertainWrite


@pytest.fixture
def runner(tmp_path):
    return KaggleCLIRunner(tmp_path / "workspace", tmp_path / "credentials", "shared-secret")


@pytest.mark.parametrize(
    "command,args",
    [
        ("auth print-access-token", []),
        ("auth revoke", []),
        ("config view", []),
        ("benchmarks auth", []),
        ("datasets", ["create"]),
        ("d create", []),
        ("datasets list", ["--unknown"]),
        ("datasets metadata", ["--up"]),
        ("datasets metadata", ["--upd"]),
        ("datasets metadata", ["--u"]),
        ("datasets metadata", ["--update=true", "owner/data"]),
        ("datasets list", ["-spayload"]),
        ("datasets list", ["--", "create"]),
        ("kernels logs", ["owner/kernel", "--follow"]),
        ("datasets download", ["owner/data", "--unzip"]),
        ("datasets download", ["owner/data", "--file", "../.env"]),
        ("datasets download", ["owner/data", "--file", "/tmp/result"]),
    ],
)
def test_unsupported_or_ambiguous_arguments_never_reach_cli(runner, command, args, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("CLI must not run")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(AgentError):
        compose_kaggle(runner, "U1").execute(
            ReadKaggle(command=command, arguments=args), "U2", "job"
        )


@pytest.mark.parametrize(
    "args",
    [
        ["--path", "../outside"],
        ["-p", "/tmp"],
        ["--path=owner"],
        ["--wp"],
        ["-powner"],
    ],
)
def test_member_cannot_choose_download_directory(runner, args):
    with pytest.raises(AgentError):
        runner.prepare("datasets download", ["owner/data", *args])


def test_upload_cannot_read_outside_workspace_or_follow_links(runner, tmp_path):
    with pytest.raises(AgentError):
        runner.prepare("competitions submit", ["titanic", "--file", "../.env"])
    folder = runner.workspace / "owner"
    folder.mkdir(parents=True)
    (folder / "secret").symlink_to(tmp_path / "outside")
    with pytest.raises(AgentError):
        runner.prepare("datasets create", ["--path", "owner"])


class Process:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 12345
        kwargs["stdout"].write(b"result shared-secret")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def wait(self, **kwargs):
        return 0


def test_read_cli_is_isolated_and_credentials_are_redacted(runner, monkeypatch):
    calls = []

    def process(*args, **kwargs):
        instance = Process(*args, **kwargs)
        calls.append(instance)
        (kwargs["cwd"] / "result.csv").write_text("value\n1\n")
        return instance

    monkeypatch.setattr(subprocess, "Popen", process)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("NOTION_TOKEN", "notion-secret")
    invocation = runner.prepare("datasets download", ["owner/data"])
    first = runner.run(invocation, "job1")
    second = runner.run(invocation, "job1")
    assert first["output"] == "result [REDACTED]"
    assert first["files"] != second["files"]
    call = calls[0]
    assert call.kwargs["stdin"] == subprocess.DEVNULL
    assert "shell" not in call.kwargs
    env = call.kwargs["env"]
    assert env["KAGGLE_API_TOKEN"] == "shared-secret"
    assert not {"SLACK_BOT_TOKEN", "OPENAI_API_KEY", "NOTION_TOKEN"} & env.keys()
    argv = call.args[0]
    assert argv[1:4] == ["-I", "-m", "kaggle"]
    assert argv[-2:] == ["--path", str(call.kwargs["cwd"])]
    assert call.kwargs["cwd"].is_relative_to(runner.workspace / "downloads")


def test_delete_cli_uses_slack_approval_instead_of_interactive_prompt(runner, monkeypatch):
    calls = []

    def process(*args, **kwargs):
        calls.append(args[0])
        return Process(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", process)
    invocation = runner.prepare("datasets delete", ["owner/data"])
    runner.run(invocation, "job")
    assert calls[0][-1] == "--yes"


def test_write_timeout_stays_uncertain_and_does_not_retry(runner, monkeypatch):
    calls = []
    kills = []

    class TimeoutProcess(Process):
        waits = 0

        def wait(self, **kwargs):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("kaggle", 1)
            return -9

    def process(*args, **kwargs):
        calls.append(args)
        return TimeoutProcess(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", process)
    monkeypatch.setattr(os, "killpg", lambda *args: kills.append(args))
    with pytest.raises(UncertainWrite):
        runner.run(runner.prepare("kernels push", []), "job")
    assert len(calls) == 1 and len(kills) == 1


def test_cli_failure_does_not_expose_stderr_or_secrets(runner, monkeypatch):
    class FailedProcess(Process):
        def wait(self, **kwargs):
            return 1

    monkeypatch.setattr(subprocess, "Popen", FailedProcess)
    with pytest.raises(AgentError) as error:
        runner.run(runner.prepare("datasets list", []), "job")
    assert "shared-secret" not in str(error.value)


def test_tool_requires_authenticated_requester(runner):
    service = compose_kaggle(runner, "U1")
    with pytest.raises(AgentError, match="依頼者"):
        service.execute(
            WriteKaggle(command="datasets delete", arguments=["owner/data"]), None, "job"
        )


def test_catalog_covers_every_supported_leaf_and_marks_side_effects(runner):
    for command, spec in runner.catalog.items():
        if any(
            part
            in {
                "submit",
                "create",
                "update",
                "init",
                "delete",
                "push",
                "run",
                "publish",
                "upload",
                "launch",
                "host-add",
                "version",
            }
            for part in command.split()
        ):
            assert spec["write"], command
    assert not runner.catalog["datasets metadata"]["write"]
    assert runner.prepare("datasets metadata", ["owner/data", "--update"]).write
    assert not runner.prepare("datasets metadata", ["owner/data"]).write


def test_cli_parser_error_is_distinct_from_auth_or_api_failure(runner, monkeypatch):
    class ArgumentErrorProcess(Process):
        def wait(self, **kwargs):
            return 2

    monkeypatch.setattr(subprocess, "Popen", ArgumentErrorProcess)
    with pytest.raises(AgentError) as error:
        runner.run(runner.prepare("competitions files", ["example", "--page-size", "wrong"]), "job")
    assert error.value.code == "kaggle_arguments"
    assert "shared-secret" not in error.value.message


@pytest.mark.parametrize(
    "command",
    [
        "competitions pages list",
        "competitions pages create",
        "competitions pages update",
        "competitions pages delete",
    ],
)
def test_page_competition_is_canonicalized_for_nested_cli_parser(runner, command):
    invocation = runner.prepare(command, ["example", "--page-name", "description"])
    assert invocation.arguments == ("--competition", "example", "--page-name", "description")
    assert runner.prepare(command, list(invocation.arguments)) == invocation
    assert "--competition" in runner.describe(command, False)["argument_notes"]


def test_page_cli_uses_explicit_competition_option(runner, monkeypatch):
    calls = []

    def process(*args, **kwargs):
        calls.append(args[0])
        return Process(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", process)
    runner.run(runner.prepare("competitions pages list", ["example", "--content"]), "job")
    argv = calls[0]
    assert argv[-3:] == ["--competition", "example", "--content"]


def test_page_command_requires_competition_instead_of_using_subcommand_as_target(runner):
    with pytest.raises(AgentError, match="--competition"):
        runner.prepare("competitions pages list", ["--content"])
