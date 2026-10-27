from typing import Annotated, Literal

from pydantic import Field

from hogwarts.reconciliation.contracts import Frozen, Identity, Sha


class FormalDispatch(Frozen):
    schema_version: Literal[1] = 1
    dispatch_id: Identity
    run_id: Identity
    owner_id: str
    actor_id: str
    generation: Annotated[int, Field(ge=1)]
    expected_version: Annotated[int, Field(ge=1)]
    profile: str
    request_sha256: Sha
    configuration_sha256: Sha
    state: Literal["DISPATCHING", "COMPLETE", "BLOCKED", "UNKNOWN"] = "DISPATCHING"
    version: Annotated[int, Field(ge=1)] = 1
    result: dict | None = None
    error_code: str | None = None

    @property
    def plan_id(self):
        # OperationalState's generic journal writer binds the SQL PK to this ID.
        return self.dispatch_id
