from abc import ABC, abstractmethod

from .models import Note, Page


class KnowledgeRepository(ABC):
    @abstractmethod
    def search(self, query: str) -> list[Page]: ...

    @abstractmethod
    def read(self, page_id: str) -> Page: ...

    @abstractmethod
    def create(self, parent_alias: str, note: Note, key: str) -> Page: ...

    @abstractmethod
    def append(self, page_id: str, note: Note, key: str) -> Page: ...

    @abstractmethod
    def update_block(
        self,
        page_id: str,
        block_id: str,
        before: str,
        after: str,
        key: str,
    ) -> Page: ...

    @abstractmethod
    def find_created(self, parent_alias: str, key: str) -> Page | None: ...

    @abstractmethod
    def find_marker(self, page_id: str, key: str) -> bool: ...

    @abstractmethod
    def block_matches(self, page_id: str, block_id: str, text: str) -> bool: ...
