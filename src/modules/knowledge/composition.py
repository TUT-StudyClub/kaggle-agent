from shared.infrastructure.http import JsonHttp

from .application.facade import KnowledgeService
from .application.use_cases.read import ReadKnowledgeUseCase
from .application.use_cases.reconcile import ReconcileKnowledgeUseCase
from .application.use_cases.write import WriteKnowledgeUseCase
from .domain.repositories import KnowledgeRepository
from .infrastructure.external_services.notion import NotionRepository


def compose_knowledge(repository: KnowledgeRepository) -> KnowledgeService:
    return KnowledgeService(
        ReadKnowledgeUseCase(repository),
        WriteKnowledgeUseCase(repository),
        ReconcileKnowledgeUseCase(repository),
    )


def build_knowledge(
    http: JsonHttp, pages: set[str], parents: dict[str, str], data_sources: set[str]
) -> KnowledgeService:
    return compose_knowledge(NotionRepository(http, pages, parents, data_sources))
