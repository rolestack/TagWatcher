# TagWatcher

A self-hosted web application that monitors Docker container images for updates and sends notifications when a new version is available.

Register your Docker hosts, and TagWatcher will periodically compare running container image tags and digests against the registry — notifying you via Slack, Discord, Telegram, and more.

## Features

- **Multi-host** — Monitor multiple Docker hosts via Unix socket or Agent (push-based)
- **Agent support** — Deploy a lightweight agent on remote hosts that cannot be accessed directly
- **Multi-tenancy** — Isolate hosts, channels, and users with Spaces and Groups
- **Flexible scheduling** — Check on a fixed interval or at specific times of day
- **Version strategies** — Control update scope: Auto / Major / Minor / Patch / Custom glob
- **Notification channels** — Slack · Discord · Telegram · Mattermost · Zulip · Microsoft Teams
- **Apply Update** — Pull and recreate containers (or queue via agent) directly from the UI
- **Live logs** — Stream container logs in real time over WebSocket
- **ACK & Snooze** — Acknowledge notifications and suppress re-alerts for a configurable period
- **OIDC / SSO** — OpenID Connect support (Keycloak, Authentik, Google, etc.)
- **Audit log** — Full record of all user and admin actions
- **Setup wizard** — Configure everything, including the database connection, from the browser on first launch

---

## Quick Start

**Requirements:** Docker, Docker Compose, a PostgreSQL instance

### 1. Create the environment file

```bash
cp .env.example .env
```

Set at minimum:

```env
APP_URL=https://tagwatcher.example.com
SECRET_KEY=replace-with-a-long-random-string
```

Generate a secure secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

### 2. Start the container

```bash
docker compose up -d
```

### 3. Complete the setup wizard

Open `http://your-server-ip:8000` in your browser and follow the wizard to configure the database connection and create the admin account.

---

## Host Types

TagWatcher supports three ways to connect to a Docker host:

| Type | Description |
|------|-------------|
| **Unix socket** | Local Docker socket (`unix:///var/run/docker.sock`). Mount the socket into the TagWatcher container. |
| **Agent** | Deploy [TagWatcher-Agent](https://github.com/rolestack/TagWatcher-Agent) on the remote host. The agent pushes container data to TagWatcher — no inbound port needed. |

> **Unix socket:** Set `DOCKER_GID` to match the socket GID:
> ```bash
> stat -c '%g' /var/run/docker.sock
> ```

### Agent Host Setup

For remote hosts where you cannot expose the Docker TCP port:

1. In the TagWatcher UI, go to a Space → **Hosts** → **Add Host** → select type **Agent**.
2. Copy the generated **Registration Token**.
3. Deploy [TagWatcher-Agent](https://github.com/rolestack/TagWatcher-Agent) on the remote host with `REGISTRATION_TOKEN` set.
4. The agent registers automatically on first startup and begins pushing container data.

---

## Database Setup

TagWatcher requires a PostgreSQL database. Create the user and database before running the setup wizard — the wizard only configures the connection URL, it does not create the database for you.

```sql
CREATE USER tagwatcher WITH PASSWORD 'changeme';
CREATE DATABASE tagwatcher OWNER tagwatcher;
\c tagwatcher
GRANT USAGE  ON SCHEMA public TO tagwatcher;
GRANT CREATE ON SCHEMA public TO tagwatcher;
```

---

## Environment Variables

| Variable | Default | Required | Description |
|----------|---------|:--------:|-------------|
| `APP_URL` | `http://localhost:8000` | ✅ | Publicly accessible URL of the service. Used to generate links in notifications. |
| `SECRET_KEY` | *(must change)* | ✅ | Session signing key. Use a long random string. |
| `POSTGRES_PASSWORD` | — | | Password for the bundled PostgreSQL container. |
| `APP_NAME` | `TagWatcher` | | Service name shown in the UI. |
| `DEBUG` | `false` | | Enable debug mode. |
| `LOG_LEVEL` | `INFO` | | Log level: `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `LOCAL_LOGIN_ENABLED` | `true` | | Allow username/password login. Set to `false` to enforce SSO-only. |
| `OIDC_PROVIDER_URL` | — | | OIDC issuer URL (e.g. `https://accounts.google.com`). |
| `OIDC_CLIENT_ID` | — | | OIDC client ID. |
| `OIDC_CLIENT_SECRET` | — | | OIDC client secret. |
| `OIDC_SCOPES` | `openid email profile` | | Scopes to request from the OIDC provider. |
| `CHECK_INTERVAL_MINUTES` | `60` | | Default update check interval when no per-host schedule is set. |
| `BEHIND_PROXY` | `false` | | Set to `true` when running behind Nginx, Traefik, or any reverse proxy. |
| `TZ` | `UTC` | | Server timezone (e.g. `Asia/Seoul`). Affects scheduled check times. |
| `SESSION_COOKIE_NAME` | `tagwatcher_session` | | Session cookie name. |
| `SESSION_MAX_AGE` | `28800` | | Session lifetime in seconds (default: 8 hours). |
| `WORKERS` | `2` | | Number of Gunicorn worker processes. |
| `BIND` | `0.0.0.0:8000` | | Address and port to bind. |
| `DOCKER_GID` | `999` | | GID of the Docker socket on the host. Required for Unix socket monitoring. |

---

## Reverse Proxy

A ready-to-use Nginx configuration is included in [`nginx.conf`](nginx.conf). It covers:

- Proxy pass to the TagWatcher app
- WebSocket upgrade for live log streaming
- `X-Forwarded-*` headers
- Gzip compression and static file caching
- Security headers
- Rate limiting on auth endpoints
- HTTPS / SSL-TLS server block (commented out — enable for production)

Set `BEHIND_PROXY=true` in `.env` when running behind any reverse proxy.

---

## Notification Channels

Channels are configured per Space under **Notification Channels**.

| Channel | Required |
|---------|----------|
| Slack | Incoming Webhook URL |
| Discord | Webhook URL |
| Telegram | Bot Token + Chat ID |
| Zulip | Site URL, Email, API Key, Stream |
| Mattermost | Incoming Webhook URL |
| Microsoft Teams | Incoming Webhook URL |

---

## Upgrade

```bash
docker compose pull
docker compose up -d
```

Database schema migrations run automatically on container startup.
