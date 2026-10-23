from shared.domain.value_object import ValueObject


class KaggleInvocation(ValueObject):
    command: str
    arguments: tuple[str, ...]
    write: bool
