"""Public API endpoints consumed by TagWatcher Agents.

No session authentication — the one-time registration token or the
per-host agent_secret acts as the credential.
"""
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Header
from pydantic import BaseModel
from sqlalchemy import select

from app.deps import DB
from app.models.docker_host import DockerHost

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent-api"])


class RegisterRequest(BaseModel):
    token: str
    hostname: str


class RegisterResponse(BaseModel):
    agent_secret: str


class ContainerSyncItem(BaseModel):
    container_id: str
    name: str
    image: str
    tag: str
    status: str
    digest: Optional[str] = None


class SyncRequest(BaseModel):
    containers: list[ContainerSyncItem]
    hostname: str = ""
    agent_version: str = ""


class SyncResponse(BaseModel):
    ok: bool


@router.post("/register", response_model=RegisterResponse)
async def register_agent(body: RegisterRequest, db: DB):
    """Called by the TagWatcher Agent on first startup to complete registration."""
    result = await db.execute(
        select(DockerHost).where(DockerHost.agent_registration_token == body.token)
    )
    host = result.scalar_one_or_none()

    if not host:
        raise HTTPException(status_code=404, detail="Invalid or expired registration token.")

    now = datetime.now(timezone.utc)
    if host.agent_registration_token_expires_at and host.agent_registration_token_expires_at < now:
        raise HTTPException(status_code=410, detail="Registration token has expired.")

    agent_secret = secrets.token_urlsafe(32)
    host.agent_secret = agent_secret
    host.agent_registration_token = None
    host.agent_registration_token_expires_at = None
    host.last_sync_error = None
    await db.commit()

    logger.info(f"Agent registered for host '{host.name}' — hostname={body.hostname}")
    return RegisterResponse(agent_secret=agent_secret)


@router.post("/sync", response_model=SyncResponse)
async def sync_agent(
    body: SyncRequest,
    db: DB,
    authorization: Optional[str] = Header(None),
):
    """Called by the agent on each sync cycle to push container data."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")

    agent_secret = authorization.removeprefix("Bearer ").strip()
    result = await db.execute(
        select(DockerHost).where(DockerHost.agent_secret == agent_secret)
    )
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=403, detail="Invalid agent secret.")

    containers = [
        {
            "container_id": c.container_id,
            "name": c.name,
            "image": f"{c.image}:{c.tag}",
            "status": c.status,
            "digest": c.digest,
        }
        for c in body.containers
    ]

    from app.services.docker_service import DockerService
    await DockerService.apply_sync_data(db, host, containers)

    if body.hostname and host.host_url != body.hostname:
        host.host_url = body.hostname
        await db.commit()

    logger.info(
        f"Agent sync received for host '{host.name}': "
        f"{len(containers)} container(s) from {body.hostname or 'unknown'}"
    )
    return SyncResponse(ok=True)
