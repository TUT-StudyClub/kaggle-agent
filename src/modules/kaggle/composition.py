from pathlib import Path

from .application.facade import KaggleService
from .application.ports.runner import KaggleRunner
from .infrastructure.cli import KaggleCLIRunner


def compose_kaggle(runner: KaggleRunner, owner_user_id: str | None) -> KaggleService:
    return KaggleService(runner, owner_user_id)


def build_kaggle(
    workspace: Path, config_dir: Path, token: str, owner_user_id: str | None
) -> KaggleService:
    return compose_kaggle(KaggleCLIRunner(workspace, config_dir, token), owner_user_id)
