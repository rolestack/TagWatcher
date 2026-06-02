import asyncio
import logging
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import docker
import docker.errors
from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.docker_host import DockerHost
from app.models.container import TrackedContainer

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=10)


# ── Image reference helpers ────────────────────────────────────────────────────

def _split_image_ref(image_ref: str) -> tuple[str, str]:
    if ":" in image_ref and not image_ref.startswith("sha256:"):
        image_name, tag = image_ref.rsplit(":", 1)
        return image_name, tag
    return image_ref, "latest"


def _extract_manifest_digest(image_attrs: dict) -> str | None:
    repo_digests = image_attrs.get("RepoDigests") or []
    if not repo_digests:
        return None
    rd = repo_digests[0]
    return rd.split("@", 1)[1] if "@" in rd else rd


# ── Container create-kwargs helpers ───────────────────────────────────────────

def _extract_bind_volumes(hcfg: dict) -> dict:
    """Parse host-bind mounts from HostConfig.Binds."""
    volumes: dict = {}
    for bind in (hcfg.get("Binds") or []):
        parts = bind.split(":")
        if len(parts) < 2:
            continue
        mode = parts[2] if len(parts) > 2 else "rw"
        volumes[parts[0]] = {"bind": parts[1], "mode": mode}
    return volumes


def _extract_named_volumes(attrs: dict, existing: dict) -> dict:
    """Parse named/anonymous volumes from container Mounts, skipping already-known binds."""
    volumes: dict = {}
    for mount in (attrs.get("Mounts") or []):
        if mount.get("Type") != "volume":
            continue
        src = mount.get("Name") or mount.get("Source")
        if not src or src in existing:
            continue
        mode = "ro" if mount.get("RW") is False else "rw"
        volumes[src] = {"bind": mount["Destination"], "mode": mode}
    return volumes


def _extract_volumes(attrs: dict, hcfg: dict) -> dict:
    bind_vols = _extract_bind_volumes(hcfg)
    named_vols = _extract_named_volumes(attrs, bind_vols)
    return {**bind_vols, **named_vols}


def _extract_ports(hcfg: dict) -> dict:
    ports: dict = {}
    for port, bindings in (hcfg.get("PortBindings") or {}).items():
        ports[port] = [(b.get("HostIp", ""), b.get("HostPort", "")) for b in bindings] if bindings else None
    return ports


def _extract_restart_policy(hcfg: dict) -> dict | None:
    rp = hcfg.get("RestartPolicy") or {}
    name = rp.get("Name")
    if not name or name == "no":
        return None
    policy: dict = {"Name": name}
    if rp.get("MaximumRetryCount"):
        policy["MaximumRetryCount"] = rp["MaximumRetryCount"]
    return policy


def _extract_extra_hosts(hcfg: dict) -> dict:
    hosts: dict = {}
    for entry in (hcfg.get("ExtraHosts") or []):
        if ":" in entry:
            h, ip = entry.split(":", 1)
            hosts[h] = ip
    return hosts


def _base_create_kwargs(cfg: dict, hcfg: dict, container_name: str, new_image: str) -> dict:
    """Build the base create-kwargs dict from container Config/HostConfig."""
    return {
        "image": new_image,
        "name": container_name,
        "detach": True,
        "environment": cfg.get("Env") or [],
        "command": cfg.get("Cmd"),
        "entrypoint": cfg.get("Entrypoint"),
        "user": cfg.get("User") or "",
        "working_dir": cfg.get("WorkingDir") or "",
        "labels": cfg.get("Labels") or {},
        "network": hcfg.get("NetworkMode", "bridge"),
    }


# ── DockerService ──────────────────────────────────────────────────────────────

class DockerService:

    # ── Client construction ────────────────────────────────────────────────────

    def _get_client_sync(self, host: DockerHost) -> docker.DockerClient:
        if not host.use_tls:
            return docker.DockerClient(base_url=host.host_url, timeout=30)
        return self._get_tls_client_sync(host)

    def _get_tls_client_sync(self, host: DockerHost) -> docker.DockerClient:
        cert_file = key_file = ca_file = None
        try:
            if host.tls_cert and host.tls_key:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                    f.write(host.tls_cert)
                    cert_file = f.name
                with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                    f.write(host.tls_key)
                    key_file = f.name
            if host.tls_ca:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".pem", delete=False) as f:
                    f.write(host.tls_ca)
                    ca_file = f.name
            tls_config = docker.tls.TLSConfig(
                client_cert=(cert_file, key_file) if cert_file and key_file else None,
                ca_cert=ca_file,
                verify=ca_file is not None,
            )
            # tcp:// → https:// so the Docker SDK uses TLS transport
            base_url = host.host_url.replace("tcp://", "https://")
            return docker.DockerClient(base_url=base_url, tls=tls_config, timeout=30)
        finally:
            for path in [cert_file, key_file, ca_file]:
                if path and os.path.exists(path):
                    os.unlink(path)

    async def get_client(self, host: DockerHost) -> docker.DockerClient:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._get_client_sync, host)

    # ── Container listing ──────────────────────────────────────────────────────

    @staticmethod
    def _parse_container_entry(c) -> dict:
        image_ref = c.image.tags[0] if c.image.tags else (c.image.id or "unknown")
        image_name, tag = _split_image_ref(image_ref)
        return {
            "container_id": c.short_id,
            "full_container_id": c.id,
            "name": c.name.lstrip("/"),
            "image": image_ref,
            "image_name": image_name,
            "tag": tag,
            "status": c.status,
            "digest": _extract_manifest_digest(c.image.attrs),
        }

    def _list_containers_sync(self, host: DockerHost) -> list[dict]:
        client = self._get_client_sync(host)
        try:
            return [self._parse_container_entry(c) for c in client.containers.list(all=True)]
        finally:
            client.close()

    async def list_containers(self, host: DockerHost) -> list[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._list_containers_sync, host)

    def _get_container_info_sync(self, host: DockerHost, container_id: str) -> dict:
        client = self._get_client_sync(host)
        try:
            c = client.containers.get(container_id)
            image_ref = c.image.tags[0] if c.image.tags else (c.image.id or "unknown")
            return {
                "container_id": c.short_id,
                "name": c.name.lstrip("/"),
                "image": image_ref,
                "status": c.status,
                "digest": c.image.id,
                "created": c.attrs.get("Created"),
                "ports": c.ports,
                "env": c.attrs.get("Config", {}).get("Env", []),
                "labels": c.labels,
            }
        finally:
            client.close()

    async def get_container_info(self, host: DockerHost, container_id: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(_executor, self._get_container_info_sync, host, container_id)

    # ── Container recreation ───────────────────────────────────────────────────

    def _build_create_kwargs(self, container, new_image: str) -> tuple[dict, list]:
        attrs = container.attrs
        cfg = attrs.get("Config", {})
        hcfg = attrs.get("HostConfig", {})
        net_settings = attrs.get("NetworkSettings", {})
        container_name = attrs.get("Name", "").lstrip("/")

        primary_network = hcfg.get("NetworkMode", "bridge")
        extra_networks = [n for n in (net_settings.get("Networks") or {}) if n != primary_network]

        create_kwargs = _base_create_kwargs(cfg, hcfg, container_name, new_image)
        self._apply_optional_kwargs(create_kwargs, attrs, hcfg)
        return create_kwargs, extra_networks

    @staticmethod
    def _apply_optional_kwargs(create_kwargs: dict, attrs: dict, hcfg: dict) -> None:
        """Append optional keys to create_kwargs only when values are present."""
        volumes = _extract_volumes(attrs, hcfg)
        ports = _extract_ports(hcfg)
        restart_policy = _extract_restart_policy(hcfg)
        extra_hosts = _extract_extra_hosts(hcfg)

        if volumes:
            create_kwargs["volumes"] = volumes
        if ports:
            create_kwargs["ports"] = ports
        if restart_policy:
            create_kwargs["restart_policy"] = restart_policy
        if extra_hosts:
            create_kwargs["extra_hosts"] = extra_hosts
        if hcfg.get("Privileged"):
            create_kwargs["privileged"] = True

    def _do_recreate(self, client, container, new_image: str, start: bool = True):
        container_name = container.attrs.get("Name", "").lstrip("/")
        create_kwargs, extra_networks = self._build_create_kwargs(container, new_image)

        container.remove(v=False)

        logger.info(f"Creating new container: {container_name} with {new_image}")
        new_container = client.containers.create(**create_kwargs)

        for net_name in extra_networks:
            try:
                client.networks.get(net_name).connect(new_container)
            except Exception as e:
                logger.warning(f"Could not connect {container_name} to network {net_name}: {e}")

        if start:
            new_container.start()
            logger.info(f"Container {container_name} started with {new_image}")

        new_container.reload()
        return new_container

    # ── Pull & recreate ────────────────────────────────────────────────────────

    def _pull_and_recreate_sync(self, host: DockerHost, docker_container_id: str, new_image: str) -> dict:
        client = self._get_client_sync(host)
        try:
            container = client.containers.get(docker_container_id)
            was_running = container.status == "running"
            logger.info(f"Pulling image: {new_image}")
            client.images.pull(new_image)
            logger.info(f"Stopping container: {container.name}")
            container.stop(timeout=30)
            new_container = self._do_recreate(client, container, new_image, start=was_running)
            return {"container_id": new_container.short_id, "status": new_container.status, "image": new_image}
        finally:
            client.close()

    @staticmethod
    def _stop_all(project_containers: list, running_ids: set) -> None:
        for c in reversed(project_containers):
            if c.id in running_ids:
                c.stop(timeout=30)

    @staticmethod
    def _start_updated(new_container, target_id: str, running_ids: set) -> None:
        if target_id in running_ids:
            new_container.start()
            logger.info(f"Started updated container: {new_container.name}")

    @staticmethod
    def _restart_others(project_containers: list, target_id: str, running_ids: set) -> None:
        for c in project_containers:
            if c.id != target_id and c.id in running_ids:
                c.start()
                logger.info(f"Started: {c.name}")

    def _compose_project_update_sync(self, host: DockerHost, docker_container_id: str, new_image: str) -> dict:
        client = self._get_client_sync(host)
        try:
            target = client.containers.get(docker_container_id)
            project = (target.labels or {}).get("com.docker.compose.project")

            all_project = sorted(
                client.containers.list(all=True, filters={"label": f"com.docker.compose.project={project}"}),
                key=lambda c: c.attrs.get("Created", ""),
            )
            running_ids = {c.id for c in all_project if c.status == "running"}

            logger.info(f"Stopping compose project '{project}' ({len(running_ids)} running containers)")
            self._stop_all(all_project, running_ids)

            logger.info(f"Pulling image: {new_image}")
            client.images.pull(new_image)

            new_container = self._do_recreate(client, target, new_image, start=False)
            self._restart_others(all_project, target.id, running_ids)
            self._start_updated(new_container, target.id, running_ids)

            new_container.reload()
            return {
                "container_id": new_container.short_id,
                "status": new_container.status,
                "image": new_image,
                "compose_project": project,
                "containers_restarted": len(running_ids),
            }
        finally:
            client.close()

    def _detect_and_run_sync(self, host: DockerHost, docker_container_id: str, new_image: str) -> dict:
        client = self._get_client_sync(host)
        try:
            c = client.containers.get(docker_container_id)
            is_compose = "com.docker.compose.project" in (c.labels or {})
        finally:
            client.close()
        if is_compose:
            return self._compose_project_update_sync(host, docker_container_id, new_image)
        return self._pull_and_recreate_sync(host, docker_container_id, new_image)

    async def pull_and_recreate(self, host: DockerHost, docker_container_id: str, new_image: str) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _executor, self._detect_and_run_sync, host, docker_container_id, new_image
        )

    # ── Log streaming ──────────────────────────────────────────────────────────

    async def stream_logs(self, host: DockerHost, container_id: str, websocket: WebSocket):
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        future = loop.run_in_executor(_executor, self._produce_logs, host, container_id, queue, loop)
        try:
            await self._consume_log_queue(queue, websocket)
        except Exception as e:
            logger.debug(f"Log stream ended: {e}")
        finally:
            future.cancel()

    def _produce_logs(self, host: DockerHost, container_id: str, queue: asyncio.Queue, loop) -> None:
        client = self._get_client_sync(host)
        try:
            c = client.containers.get(container_id)
            for line in c.logs(stream=True, follow=True, tail=100):
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                asyncio.run_coroutine_threadsafe(queue.put(line), loop)
        except Exception as e:
            asyncio.run_coroutine_threadsafe(queue.put(f"ERROR: {e}"), loop)
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop)
            client.close()

    @staticmethod
    async def _consume_log_queue(queue: asyncio.Queue, websocket: WebSocket) -> None:
        while True:
            try:
                line = await asyncio.wait_for(queue.get(), timeout=30.0)
            except asyncio.TimeoutError:
                await websocket.send_text("")
                continue
            if line is None:
                break
            await websocket.send_text(line)

    # ── Container sync ─────────────────────────────────────────────────────────

    async def sync_containers(self, db: AsyncSession, host: DockerHost):
        if host.host_type == "agent":
            logger.debug(f"Skipping sync for agent host '{host.name}' — agent pushes data")
            return
        try:
            containers = await self.list_containers(host)
        except Exception as e:
            logger.error(f"Failed to list containers on host {host.name}: {e}")
            raise

        await self.apply_sync_data(db, host, containers)

    @staticmethod
    async def apply_sync_data(db: AsyncSession, host: DockerHost, containers: list[dict]) -> None:
        """Upsert container records from a container list (used by both TCP/Unix sync and agent push)."""
        result = await db.execute(
            select(TrackedContainer).where(TrackedContainer.docker_host_id == host.id)
        )
        existing_by_name = DockerService._build_existing_map(result.scalars().all())
        seen_names = {c["name"] for c in containers}

        for c_data in containers:
            DockerService._upsert_container(db, existing_by_name, c_data, host.id)

        for cname, tc in existing_by_name.items():
            if cname not in seen_names and tc.status != "removed":
                tc.status = "removed"

        host.last_synced_at = datetime.now(timezone.utc)
        host.last_sync_error = None
        await db.commit()
        logger.debug(f"Synced {len(containers)} container(s) for host '{host.name}'")

    @staticmethod
    def _build_existing_map(rows) -> dict[str, TrackedContainer]:
        existing: dict[str, TrackedContainer] = {}
        for c in rows:
            if c.name not in existing or c.status != "removed":
                existing[c.name] = c
        return existing

    @staticmethod
    def _upsert_container(db, existing_by_name: dict, c_data: dict, host_id) -> None:
        cname = c_data["name"]
        image_name, tag = _split_image_ref(c_data["image"])
        if cname in existing_by_name:
            tc = existing_by_name[cname]
            tc.container_id = c_data["container_id"]
            tc.image = image_name
            tc.tag = tag
            tc.status = c_data["status"]
            tc.namespace = c_data.get("namespace") or None
            new_digest = c_data.get("digest")
            tc.digest = new_digest
            # Auto-clear update flag when agent reports the container is now running
            # the latest known digest (update was applied successfully).
            if new_digest and tc.latest_digest and new_digest == tc.latest_digest:
                tc.has_update = False
        else:
            tc = TrackedContainer(
                docker_host_id=host_id,
                container_id=c_data["container_id"],
                name=cname,
                namespace=c_data.get("namespace") or None,
                image=image_name,
                tag=tag,
                status=c_data["status"],
                digest=c_data.get("digest"),
            )
            db.add(tc)
            existing_by_name[cname] = tc
