"""Public API endpoints consumed by TagWatcher Agents.

No session authentication — the one-time registration token or the
per-host agent_secret acts as the credential.
"""
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.deps import DB
from app.models.docker_host import DockerHost

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent-api"])


class RegisterRequest(BaseModel):
    token: str
    agent_url: str
    hostname: str


class RegisterResponse(BaseModel):
    agent_secret: str


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
    host.host_url = body.agent_url.rstrip("/")
    host.agent_secret = agent_secret
    host.agent_registration_token = None
    host.agent_registration_token_expires_at = None
    host.last_sync_error = None
    await db.commit()

    logger.info(
        f"Agent registered for host '{host.name}' — agent_url={body.agent_url} hostname={body.hostname}"
    )
    return RegisterResponse(agent_secret=agent_secret)
