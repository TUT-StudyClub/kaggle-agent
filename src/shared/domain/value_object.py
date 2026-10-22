from pydantic import BaseModel, ConfigDict


class ValueObject(BaseModel):
    """不変の値。外部からの未知フィールドは受け付けない。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
