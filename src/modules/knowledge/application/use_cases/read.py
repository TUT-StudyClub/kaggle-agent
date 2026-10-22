from modules.knowledge.application.dto.commands import (
    ReadPage,
    SearchPages,
)
from modules.knowledge.domain.repositories import KnowledgeRepository
from shared.errors import AgentError


class ReadKnowledgeUseCase:
    def __init__(self, repository: KnowledgeRepository):
        self.repository = repository

    def execute(self, action: object, source_url: str, key: str) -> dict[str, object]:
        match action:
            case SearchPages():
                return {"pages": [p.model_dump() for p in self.repository.search(action.query)]}
            case ReadPage():
                return {"page": self.repository.read(action.page_id).model_dump()}
            case _:
                raise AgentError("unsupported_action", "未対応の記録操作です。")
