"""Public API endpoints consumed by TagWatcher Agents.

No session authentication — the one-time registration token or the
per-host agent_secret acts as the credential.
"""
import asyncio
import ipaddress
import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Header, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.deps import DB
from app.models.docker_host import DockerHost

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/agent", tags=["agent-api"])


def _get_client_ip(request: Request) -> str:
    """Return the real client IP, respecting X-Forwarded-For if present."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def _is_ip_allowed(ip_str: str, allowed_cidrs: str | None) -> bool:
    """Return True if ip_str falls within any of the allowed CIDR ranges."""
    raw = (allowed_cidrs or "0.0.0.0/0").strip()
    if not raw:
        return True
    try:
        client_ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for entry in raw.replace(",", "\n").splitlines():
        entry = entry.strip()
        if not entry:
            continue
        try:
            if client_ip in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


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


# Pending container updates per host, keyed by str(host.id).
# Populated by the container update endpoint; consumed and cleared on next agent sync.
_agent_pending_updates: dict[str, list[dict]] = {}

# Active log-stream subscribers: container_id → list of asyncio.Queue (one per WebSocket).
_agent_log_subscribers: dict[str, list] = {}
# Which containers each host should send logs for (set when a WebSocket is open).
_agent_log_requests: dict[str, set] = {}  # host_id → {container_id, ...}


class SyncResponse(BaseModel):
    ok: bool
    pending_updates: list[dict] = []
    request_logs: list[str] = []


class LogChunk(BaseModel):
    container_id: str
    lines: list[str]


class LogDataRequest(BaseModel):
    chunks: list[LogChunk]


@router.post("/register", response_model=RegisterResponse)
async def register_agent(body: RegisterRequest, request: Request, db: DB):
    """Called by the TagWatcher Agent on first startup to complete registration."""
    result = await db.execute(
        select(DockerHost).where(DockerHost.agent_registration_token == body.token)
    )
    host = result.scalar_one_or_none()

    if not host:
        raise HTTPException(
            status_code=404,
            detail=(
                "Registration token not found or already used. "
                "Tokens are single-use — generate a new one in TagWatcher and update REGISTRATION_TOKEN. "
                "Tip: mount /data as a persistent volume so the agent secret survives restarts."
            ),
        )

    now = datetime.now(timezone.utc)
    if host.agent_registration_token_expires_at and host.agent_registration_token_expires_at < now:
        raise HTTPException(status_code=410, detail="Registration token has expired.")

    client_ip = _get_client_ip(request)
    if not _is_ip_allowed(client_ip, host.agent_allowed_cidrs):
        logger.warning(f"Agent registration blocked: IP {client_ip} not in allow list for host '{host.name}'")
        raise HTTPException(status_code=403, detail=f"IP address {client_ip} is not in the allowed list.")

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
    request: Request,
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

    client_ip = _get_client_ip(request)
    if not _is_ip_allowed(client_ip, host.agent_allowed_cidrs):
        logger.warning(f"Agent sync blocked: IP {client_ip} not in allow list for host '{host.name}'")
        raise HTTPException(status_code=403, detail=f"IP address {client_ip} is not in the allowed list.")

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

    pending = _agent_pending_updates.pop(str(host.id), [])
    request_logs = list(_agent_log_requests.get(str(host.id), set()))

    logger.info(
        f"Agent sync received for host '{host.name}': "
        f"{len(containers)} container(s) from {body.hostname or 'unknown'}"
        + (f", {len(pending)} pending update(s)" if pending else "")
        + (f", {len(request_logs)} log stream(s)" if request_logs else "")
    )
    return SyncResponse(ok=True, pending_updates=pending, request_logs=request_logs)


@router.post("/log-data")
async def receive_log_data(
    body: LogDataRequest,
    request: Request,
    db: DB,
    authorization: Optional[str] = Header(None),
):
    """Receive log chunks pushed by an agent and fan them out to waiting WebSockets."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Unauthorized")
    agent_secret = authorization.removeprefix("Bearer ").strip()
    result = await db.execute(select(DockerHost).where(DockerHost.agent_secret == agent_secret))
    host = result.scalar_one_or_none()
    if not host:
        raise HTTPException(status_code=403, detail="Invalid agent secret.")

    client_ip = _get_client_ip(request)
    if not _is_ip_allowed(client_ip, host.agent_allowed_cidrs):
        raise HTTPException(status_code=403, detail=f"IP address {client_ip} is not in the allowed list.")

    for chunk in body.chunks:
        subs = _agent_log_subscribers.get(chunk.container_id, [])
        for q in subs:
            for line in chunk.lines:
                await q.put(line)
    return {"ok": True}
