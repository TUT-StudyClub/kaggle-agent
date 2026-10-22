from modules.knowledge.application.dto.commands import AppendPage, CreatePage, UpdateBlock
from modules.knowledge.domain.repositories import KnowledgeRepository


class ReconcileKnowledgeUseCase:
    def __init__(self, repository: KnowledgeRepository):
        self.repository = repository

    def execute(self, action: object, key: str) -> dict[str, object] | None:
        match action:
            case CreatePage():
                page = self.repository.find_created(action.parent_alias, key)
            case AppendPage():
                page = (
                    self.repository.read(action.page_id)
                    if self.repository.find_marker(action.page_id, key)
                    else None
                )
            case UpdateBlock():
                page = (
                    self.repository.read(action.page_id)
                    if self.repository.block_matches(action.page_id, action.block_id, action.after)
                    else None
                )
            case _:
                page = None
        return {"page": page.model_dump(exclude={"text"})} if page else None
