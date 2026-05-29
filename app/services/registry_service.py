import fnmatch
import logging
import re
from typing import Optional
from datetime import datetime

import httpx
from packaging.version import Version, InvalidVersion

logger = logging.getLogger(__name__)


def _log_registry_error(log: logging.Logger, msg: str, exc: Exception) -> None:
    """Log registry HTTP errors at DEBUG for 401/404 (private/missing image) and WARNING for others."""
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (401, 404):
        log.debug(f"{msg}: {exc}")
    else:
        log.warning(f"{msg}: {exc}")


ROLLING_TAGS = {"latest", "stable", "edge", "nightly", "main", "master", "develop", "development"}

DOCKERHUB_API = "https://hub.docker.com/v2"
DOCKERHUB_REGISTRY = "https://registry-1.docker.io/v2"

# Token endpoint templates for known registries (anonymous pull)
_REGISTRY_TOKEN_URLS: dict[str, str] = {
    "ghcr.io": "https://ghcr.io/token?scope=repository:{repo}:pull&service=ghcr.io",
    "lscr.io": "https://lscr.io/token?scope=repository:{repo}:pull&service=lscr.io",
}


class RegistryService:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)

    async def close(self):
        await self._client.aclose()

    # ── Image reference parsing ───────────────────────────────────────────────

    def _parse_image_parts(self, image: str) -> tuple[str, str, str]:
        """
        Parse image reference into (registry_base_url, full_repo_path, repo_name).

        "nginx"                          → ("", "library", "nginx")
        "myuser/myapp"                   → ("", "myuser/myapp", "myapp")
        "ghcr.io/owner/repo"             → ("https://ghcr.io/v2", "owner/repo", "repo")
        """
        parts = image.split("/")
        if len(parts) >= 2 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
            registry_host = parts[0]
            rest = "/".join(parts[1:])
            return f"https://{registry_host}/v2", rest, parts[-1]
        elif len(parts) == 1:
            return "", "library", parts[0]
        else:
            return "", "/".join(parts), parts[-1]

    def _registry_host(self, registry_base: str) -> str:
        """Extract hostname from a registry base URL like 'https://ghcr.io/v2'."""
        return registry_base.removeprefix("https://").removesuffix("/v2")

    # ── Version parsing (suffix-aware) ───────────────────────────────────────

    def _parse_version_and_suffix(self, tag: str) -> tuple[Optional[Version], str]:
        """
        Parse a tag into (Version, suffix_string).

        Handles common patterns:
          "8.0.37"        → (Version("8.0.37"), "")
          "6.39.0-alpine" → (Version("6.39.0"), "-alpine")
          "2.39.2-alpine" → (Version("2.39.2"), "-alpine")
          "11.5-2"        → (Version("11.5.2"), "")   ← numeric dash absorbed
          "29.2.7"        → (Version("29.2.7"), "")
          "v2.1.0"        → (Version("2.1.0"),  "")
        """
        t = tag.lstrip("v")
        m = re.match(r'^(\d+(?:\.\d+)*)(-.+)?$', t)
        if not m:
            return None, ""

        ver_str = m.group(1)
        suffix = m.group(2) or ""

        # A purely-numeric dash suffix (e.g. -2 in 11.5-2) is treated as a patch level
        if suffix and re.match(r'^-\d+$', suffix):
            ver_str = f"{ver_str}.{suffix[1:]}"
            suffix = ""

        try:
            return Version(ver_str), suffix
        except InvalidVersion:
            return None, suffix

    # ── Registry authentication ───────────────────────────────────────────────

    async def _get_dockerhub_token(self, repo: str) -> Optional[str]:
        try:
            resp = await self._client.get(
                f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repo}:pull"
            )
            resp.raise_for_status()
            return resp.json().get("token")
        except Exception as e:
            logger.warning(f"Failed to get Docker Hub token for {repo}: {e}")
            return None

    async def _get_registry_token(self, registry_host: str, repo: str) -> Optional[str]:
        """Obtain an anonymous bearer token for a known container registry."""
        template = _REGISTRY_TOKEN_URLS.get(registry_host)
        if not template:
            return None
        try:
            resp = await self._client.get(template.format(repo=repo))
            resp.raise_for_status()
            data = resp.json()
            return data.get("token") or data.get("access_token")
        except Exception as e:
            logger.warning(f"Failed to get registry token for {registry_host}/{repo}: {e}")
            return None

    async def _get_token_from_challenge(self, www_authenticate: str, fallback_scope: str) -> Optional[str]:
        """Parse a Bearer WWW-Authenticate challenge and fetch a token."""
        realm_m = re.search(r'realm="([^"]+)"', www_authenticate, re.IGNORECASE)
        service_m = re.search(r'service="([^"]+)"', www_authenticate, re.IGNORECASE)
        scope_m = re.search(r'scope="([^"]+)"', www_authenticate, re.IGNORECASE)
        if not realm_m:
            return None
        params: dict[str, str] = {"scope": scope_m.group(1) if scope_m else fallback_scope}
        if service_m:
            params["service"] = service_m.group(1)
        try:
            resp = await self._client.get(realm_m.group(1), params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("token") or data.get("access_token")
        except Exception as e:
            logger.warning(f"Token fetch from challenge failed: {e}")
            return None

    async def _get_oci_token(self, registry_base: str, host: str, namespace: str) -> Optional[str]:
        """Get a bearer token for an OCI registry, using challenge-response if hardcoded URL fails."""
        token = await self._get_registry_token(host, namespace)
        if token:
            return token
        # Fall back: probe the tags endpoint to get the WWW-Authenticate challenge
        try:
            r = await self._client.get(
                f"{registry_base}/{namespace}/tags/list",
                headers={},
                follow_redirects=True,
            )
            if r.status_code == 401:
                www_auth = r.headers.get("www-authenticate", "")
                if "bearer" in www_auth.lower():
                    return await self._get_token_from_challenge(
                        www_auth, f"repository:{namespace}:pull"
                    )
        except Exception:
            pass
        return None

    # ── Registry queries ──────────────────────────────────────────────────────

    # Accept header that covers single-arch manifests, multi-arch manifest lists, and OCI index
    _MANIFEST_ACCEPT = (
        "application/vnd.docker.distribution.manifest.list.v2+json, "
        "application/vnd.oci.image.index.v1+json, "
        "application/vnd.docker.distribution.manifest.v2+json, "
        "application/vnd.oci.image.manifest.v1+json"
    )

    async def get_current_digest(self, image: str, tag: str) -> Optional[str]:
        """Return the manifest digest for image:tag from the registry."""
        registry_base, namespace, repo_name = self._parse_image_parts(image)

        if not registry_base:
            # Docker Hub
            repo = namespace if "/" in namespace else f"{namespace}/{repo_name}"
            token = await self._get_dockerhub_token(repo)
            if not token:
                return None
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": self._MANIFEST_ACCEPT,
            }
            try:
                resp = await self._client.head(
                    f"{DOCKERHUB_REGISTRY}/{repo}/manifests/{tag}",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.headers.get("Docker-Content-Digest")
            except Exception as e:
                _log_registry_error(logger, f"Failed to get digest for {image}:{tag}", e)
                return None
        else:
            host = self._registry_host(registry_base)
            token = await self._get_oci_token(registry_base, host, namespace)
            headers: dict = {"Accept": self._MANIFEST_ACCEPT}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                resp = await self._client.head(
                    f"{registry_base}/{namespace}/manifests/{tag}",
                    headers=headers,
                )
                resp.raise_for_status()
                return resp.headers.get("Docker-Content-Digest")
            except Exception as e:
                _log_registry_error(logger, f"Failed to get digest for {image}:{tag} from {host}", e)
                return None

    async def _fetch_dockerhub_tags(self, repo: str) -> list[dict]:
        """Paginate through Docker Hub REST API tags for a repository."""
        tags: list[dict] = []
        url: Optional[str] = (
            f"{DOCKERHUB_API}/repositories/{repo}/tags/?page_size=100&ordering=last_updated"
        )
        while url:
            try:
                resp = await self._client.get(url)
                resp.raise_for_status()
                data = resp.json()
                for t in data.get("results", []):
                    tags.append({
                        "name": t["name"],
                        "last_updated": t.get("last_updated"),
                        "digest": t.get("digest"),
                    })
                url = data.get("next")
            except Exception as e:
                _log_registry_error(logger, f"Failed to fetch Docker Hub tags for {repo}", e)
                break
        return tags

    async def _fetch_oci_tags(self, registry_base: str, host: str, namespace: str) -> list[dict]:
        """Paginate through OCI / private registry tags/list endpoint."""
        token = await self._get_oci_token(registry_base, host, namespace)
        headers: dict = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        all_tags: list[str] = []
        next_url: Optional[str] = f"{registry_base}/{namespace}/tags/list"
        while next_url:
            try:
                resp = await self._client.get(next_url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                all_tags.extend(data.get("tags", []))
                link = resp.headers.get("link", "")
                next_m = re.search(r'<([^>]+)>;\s*rel="next"', link)
                if next_m:
                    path = next_m.group(1)
                    next_url = f"https://{host}{path}" if path.startswith("/") else path
                else:
                    next_url = None
            except Exception as e:
                _log_registry_error(logger, f"Failed to fetch tags from {host} for {namespace}", e)
                break
        return [{"name": t, "last_updated": None, "digest": None} for t in all_tags]

    async def get_available_tags(self, image: str) -> list[dict]:
        """Return all tags for an image with metadata (name, last_updated, digest)."""
        registry_base, namespace, repo_name = self._parse_image_parts(image)
        if not registry_base:
            repo = namespace if "/" in namespace else f"{namespace}/{repo_name}"
            return await self._fetch_dockerhub_tags(repo)
        host = self._registry_host(registry_base)
        return await self._fetch_oci_tags(registry_base, host, namespace)

    # ── Update detection ──────────────────────────────────────────────────────

    async def _get_current_or_none(
        self, image: str, tag: str
    ) -> Optional[tuple[str, Optional[str], None]]:
        """Fetch current digest and return a find_latest_version-compatible tuple, or None."""
        digest = await self.get_current_digest(image, tag)
        return (tag, digest, None) if digest else None

    @staticmethod
    def _is_channel_tag(current_version: Version, current_suffix: str) -> bool:
        """Return True for floating channel tags (digest-only comparison, no version search)."""
        short_no_suffix = len(current_version.release) <= 2 and current_suffix == ""
        short_with_suffix = len(current_version.release) == 1 and current_suffix != ""
        return short_no_suffix or short_with_suffix

    @staticmethod
    def _resolve_effective_strategy(
        strategy: str, custom_pattern: Optional[str], current_version: Version
    ) -> str:
        if strategy == "custom" and not custom_pattern:
            return "auto"
        if strategy != "auto":
            return strategy
        return "minor" if len(current_version.release) >= 2 else "major"

    @staticmethod
    def _passes_strategy_filter(
        tag_name: str,
        v: Version,
        current_version: Version,
        current_suffix: str,
        tag_suffix: str,
        effective_strategy: str,
        custom_pattern: Optional[str],
    ) -> bool:
        if tag_suffix != current_suffix:
            return False
        if v.major > 10_000:
            return False
        if effective_strategy == "custom":
            return bool(custom_pattern and fnmatch.fnmatch(tag_name, custom_pattern))
        if effective_strategy == "major":
            return v.major > current_version.major
        if effective_strategy == "minor":
            return v.major == current_version.major
        if effective_strategy == "patch":
            return v.major == current_version.major and v.minor == current_version.minor
        return True

    @staticmethod
    def _parse_release_date(best_date: Optional[str]) -> Optional[str]:
        if not best_date:
            return None
        try:
            return datetime.fromisoformat(best_date.replace("Z", "+00:00")).isoformat()
        except Exception:
            return best_date

    async def find_latest_version(
        self, image: str, current_tag: str, strategy: str = "auto",
        custom_pattern: str | None = None,
    ) -> Optional[tuple[str, Optional[str], Optional[str]]]:
        """
        Check whether a newer version of image:current_tag is available.

        Returns (latest_tag, latest_digest, release_date_iso_str) or None if
        the registry could not be reached / no update information is available.

        Rolling tags (latest, stable, …):
            Returns the current registry digest so the caller can detect if it
            changed since the last check.

        Versioned tags (8.0.37, 6.39.0-alpine, 11.5-2, …):
            Finds the highest tag with the same suffix (-alpine, "", …).

        Strategy controls version comparison scope:
            auto        — 1-segment tag (e.g. "17") → unrestricted;
                          2+ segments (e.g. "16.2") → same_major
            unrestricted — compare all versions (find absolute highest)
            same_major  — only compare versions within the same major (16.x)
            same_minor  — only compare versions within the same major.minor (16.2.x)
            custom      — only consider tags matching custom_pattern glob (e.g. "29.2.*")
        """
        if current_tag.lower() in ROLLING_TAGS:
            return await self._get_current_or_none(image, current_tag)

        current_version, current_suffix = self._parse_version_and_suffix(current_tag)

        if current_version is None:
            return await self._get_current_or_none(image, current_tag)

        if self._is_channel_tag(current_version, current_suffix):
            return await self._get_current_or_none(image, current_tag)

        effective_strategy = self._resolve_effective_strategy(strategy, custom_pattern, current_version)

        tags = await self.get_available_tags(image)
        if not tags:
            return None

        best_tag: Optional[str] = None
        best_version = current_version
        best_date: Optional[str] = None
        best_digest: Optional[str] = None

        for tag_info in tags:
            tag_name = tag_info["name"]
            v, tag_suffix = self._parse_version_and_suffix(tag_name)
            if v is None:
                continue
            if not self._passes_strategy_filter(
                tag_name, v, current_version, current_suffix, tag_suffix,
                effective_strategy, custom_pattern
            ):
                continue
            if v > best_version:
                best_version = v
                best_tag = tag_name
                best_date = tag_info.get("last_updated")
                best_digest = tag_info.get("digest")

        if best_tag is None:
            return (current_tag, None, None)

        return (best_tag, best_digest, self._parse_release_date(best_date))
