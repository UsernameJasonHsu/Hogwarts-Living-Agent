from datetime import datetime
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field


class Heartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    lease_token: Annotated[str, Field(min_length=1, max_length=100)]
    generation: Annotated[int, Field(ge=0)]
    expected_version: Annotated[int, Field(ge=1)]


class LeaseInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str
    generation: int
    version: int
    expires_at: datetime | None
    revoked: bool
    stop_state: str


class LeaseController(Protocol):
    def heartbeat(self, actor, run_id: str, data: Heartbeat, key: str) -> LeaseInfo: ...
    def revoke(self, actor, run_id: str, generation: int, key: str) -> LeaseInfo: ...
    def expire(self, actor, run_id: str, generation: int, key: str) -> LeaseInfo: ...
