from modules.knowledge.application.dto.commands import (
    AppendPage,
    CreatePage,
    UpdateBlock,
)
from modules.knowledge.domain.models import Note
from modules.knowledge.domain.repositories import KnowledgeRepository
from shared.errors import AgentError


class WriteKnowledgeUseCase:
    def __init__(self, repository: KnowledgeRepository):
        self.repository = repository

    def execute(self, action: object, source_url: str, key: str) -> dict[str, object]:
        match action:
            case CreatePage():
                page = self.repository.create(
                    action.parent_alias,
                    Note(title=action.title, body=action.body, source_url=source_url),
                    key,
                )
            case AppendPage():
                page = self.repository.append(
                    action.page_id,
                    Note(title=action.title, body=action.body, source_url=source_url),
                    key,
                )
            case UpdateBlock():
                page = self.repository.update_block(
                    action.page_id,
                    action.block_id,
                    action.before,
                    action.after,
                    key,
                )
            case _:
                raise AgentError("unsupported_action", "未対応の記録操作です。")
        return {"page": page.model_dump(exclude={"text"})}
