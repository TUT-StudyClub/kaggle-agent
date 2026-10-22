from .dto.commands import ReadPage, SearchPages
from .use_cases.read import ReadKnowledgeUseCase
from .use_cases.reconcile import ReconcileKnowledgeUseCase
from .use_cases.write import WriteKnowledgeUseCase


class KnowledgeService:
    """記録操作の公開窓口。Repositoryは外部へ公開しない。"""

    def __init__(
        self,
        read: ReadKnowledgeUseCase,
        write: WriteKnowledgeUseCase,
        reconcile: ReconcileKnowledgeUseCase,
    ):
        self._read = read
        self._write = write
        self._reconcile = reconcile

    def execute(self, action: object, source_url: str, key: str) -> dict[str, object]:
        if isinstance(action, (SearchPages, ReadPage)):
            return self._read.execute(action, source_url, key)
        return self._write.execute(action, source_url, key)

    def reconcile(self, action: object, key: str) -> dict[str, object] | None:
        return self._reconcile.execute(action, key)

    def ensure_page_access(self, page_id: str) -> None:
        self._read.execute(ReadPage(page_id=page_id), "", "")
