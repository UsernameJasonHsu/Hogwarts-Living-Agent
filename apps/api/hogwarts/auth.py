import os
import secrets
from dataclasses import dataclass
from typing import Annotated

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    id: str
    owner_id: str
    role: str
    agent_key: str | None = None


def authenticate(credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)]):
    human = os.getenv("HUMAN_API_TOKEN", "")
    worker = os.getenv("WORKER_API_TOKEN", "")
    if (
        len(human) < 32
        or len(worker) < 32
        or human == worker
        or not human.isascii()
        or not worker.isascii()
    ):
        raise HTTPException(
            503, "configure distinct ASCII human and worker tokens (32+ characters)"
        )
    if credentials is None:
        raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
    if not credentials.credentials.isascii():
        raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Bearer"})
    owner = os.getenv("OWNER_ID", "local-owner")
    if secrets.compare_digest(credentials.credentials, human):
        return Principal("human:" + owner, owner, "human")
    if secrets.compare_digest(credentials.credentials, worker):
        key = os.getenv("WORKER_AGENT_KEY", "engineer")
        return Principal("worker:" + key, owner, "worker", key)
    raise HTTPException(401, "invalid credentials", headers={"WWW-Authenticate": "Bearer"})
