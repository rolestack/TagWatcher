import logging
import os
from typing import Optional, Any
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_TEAMS_MSG_CARD_TYPE = "@type"
_VIEW_IN_TAGWATCHER = "View in TagWatcher"

def _format_time(dt: datetime) -> str:
    """Format a UTC datetime in the system timezone (TZ env var)."""
    if dt is None:
        return ""
    try:
        tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    except ZoneInfoNotFoundError:
        tz = ZoneInfo("UTC")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(tz)
    return local.strftime(f"%Y-%m-%d %H:%M {local.strftime('%Z')}")

import httpx
from fastapi import HTTPException

from app.models.notification import NotificationChannel, ChannelType
from app.models.container import TrackedContainer
from app.security import validate_webhook_url

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self):
        self._client = httpx.AsyncClient(timeout=15.0)

    async def close(self):
        await self._client.aclose()

    def _build_notification_content(
        self, container: TrackedContainer, app_url: str,
        host_name: str = "", space_name: str = "", check_time: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Build a platform-agnostic notification payload."""
        container_url = f"{app_url.rstrip('/')}/containers/{container.id}"
        current = f"{container.tag}"
        if container.digest:
            current += f" ({container.digest[:12]})"

        new_version = container.latest_tag or "unknown"
        if container.latest_digest:
            new_version += f" ({container.latest_digest[:12]})"

        release_date = ""
        if container.release_date:
            try:
                if isinstance(container.release_date, str):
                    release_date = container.release_date[:10]
                else:
                    release_date = container.release_date.strftime("%Y-%m-%d")
            except Exception:
                release_date = str(container.release_date)

        check_time_str = _format_time(check_time) if check_time else ""

        # Namespace is only set for Kubernetes containers.
        namespace = container.namespace or ""

        return {
            "container_name": container.name,
            "namespace": namespace,
            "image": container.image,
            "current_version": current,
            "new_version": new_version,
            "release_date": release_date,
            "container_url": container_url,
            "title": f"Update available: {container.name}",
            "summary": (
                f"Container **{container.name}** ({container.image}) "
                f"has an update available.\n"
                + (f"Namespace: `{namespace}`\n" if namespace else "")
                + f"Current: `{current}`\n"
                + f"Latest: `{new_version}`"
                + (f"\nReleased: {release_date}" if release_date else "")
            ),
            "host_name": host_name,
            "space_name": space_name,
            "check_time": check_time_str,
        }

    async def _dispatch(self, channel: NotificationChannel, content: dict, aggregated: bool = False) -> None:
        """Dispatch a notification to the appropriate channel handler."""
        handlers = {
            ChannelType.slack:      (self._send_aggregated_slack,      self.send_slack),
            ChannelType.discord:    (self._send_aggregated_discord,    self.send_discord),
            ChannelType.telegram:   (self._send_aggregated_telegram,   self.send_telegram),
            ChannelType.zulip:      (self._send_aggregated_zulip,      self.send_zulip),
            ChannelType.mattermost: (self._send_aggregated_mattermost, self.send_mattermost),
            ChannelType.teams:      (self._send_aggregated_teams,      self.send_teams),
        }
        pair = handlers.get(channel.channel_type)
        if not pair:
            logger.warning(f"Unknown channel type: {channel.channel_type}")
            return
        handler = pair[0] if aggregated else pair[1]
        await handler(channel.config, content)

    async def send_update_notification(
        self, channel: NotificationChannel, container: TrackedContainer, app_url: str,
        host_name: str = "", space_name: str = "", check_time: Optional[datetime] = None,
    ):
        content = self._build_notification_content(container, app_url, host_name, space_name, check_time)
        try:
            await self._dispatch(channel, content, aggregated=False)
        except Exception as e:
            logger.error(f"Failed to send notification via {channel.channel_type}: {e}")
            raise

    def _build_aggregated_content(
        self, containers: list, app_url: str,
        host_name: str = "", space_name: str = "", check_time: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """Build a combined notification payload for multiple containers."""
        base_url = app_url.rstrip("/")
        count = len(containers)
        title = f"Updates available: {count} container{'s' if count != 1 else ''}"

        lines = []
        for c in containers:
            current = c.tag
            latest = c.latest_tag or "?"
            rd = ""
            if c.release_date:
                try:
                    rd = f" — released {c.release_date.strftime('%Y-%m-%d')}"
                except Exception:
                    pass
            url = f"{base_url}/containers/{c.id}"
            lines.append({"name": c.name, "current": current, "latest": latest,
                          "release": rd, "url": url, "image": c.image})

        summary_text = "\n".join(
            f"• {l['name']} (`{l['current']}` → `{l['latest']}`){l['release']}"
            for l in lines
        )

        check_time_str = _format_time(check_time) if check_time else ""

        return {
            "title": title,
            "count": count,
            "lines": lines,
            "summary": summary_text,
            "host_name": host_name,
            "space_name": space_name,
            "check_time": check_time_str,
        }

    async def send_aggregated_update_notification(
        self, channel: NotificationChannel, containers: list, app_url: str,
        host_name: str = "", space_name: str = "", check_time: Optional[datetime] = None,
    ):
        """Send one combined notification for multiple updated containers."""
        content = self._build_aggregated_content(containers, app_url, host_name, space_name, check_time)
        try:
            await self._dispatch(channel, content, aggregated=True)
        except Exception as e:
            logger.error(f"Aggregated notification failed via {channel.channel_type}: {e}")
            raise

    def _context_lines(self, content: dict) -> str:
        """Return Space/Host/시간 context as newline-joined plain text."""
        parts = []
        if content.get("space_name"):
            parts.append(f"Space: {content['space_name']}")
        if content.get("host_name"):
            parts.append(f"Host: {content['host_name']}")
        if content.get("check_time"):
            parts.append(f"시간: {content['check_time']}")
        return "\n".join(parts)

    async def _send_aggregated_slack(self, config: dict, content: dict):
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Slack config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Slack webhook URL invalid: {err}")

        blocks: list = [
            {"type": "header", "text": {"type": "plain_text", "text": content["title"], "emoji": True}},
        ]
        ctx = self._context_lines(content)
        if ctx:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]})
        for l in content["lines"]:
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn",
                         "text": f"*<{l['url']}|{l['name']}>*  `{l['current']}` → `{l['latest']}`{l['release']}"},
            })
        resp = await self._client.post(webhook_url, json={"blocks": blocks})
        resp.raise_for_status()

    async def _send_aggregated_discord(self, config: dict, content: dict):
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Discord config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Discord webhook URL invalid: {err}")

        ctx = self._context_lines(content)
        items_text = "\n".join(
            f"**[{l['name']}]({l['url']})** `{l['current']}` → `{l['latest']}`{l['release']}"
            for l in content["lines"]
        )
        description = f"{ctx}\n\n{items_text}" if ctx else items_text
        embed = {"title": content["title"], "color": 0xF59E0B, "description": description}
        resp = await self._client.post(webhook_url, json={"embeds": [embed]})
        resp.raise_for_status()

    async def _send_aggregated_telegram(self, config: dict, content: dict):
        bot_token = config.get("bot_token")
        chat_id = config.get("chat_id")
        if not bot_token or not chat_id:
            raise ValueError("Telegram config missing 'bot_token' or 'chat_id'")

        ctx = self._context_lines(content)
        lines_text = "\n".join(
            f"• <a href='{l['url']}'>{l['name']}</a>: <code>{l['current']}</code> → <code>{l['latest']}</code>{l['release']}"
            for l in content["lines"]
        )
        body = f"{ctx}\n\n{lines_text}" if ctx else lines_text
        text = f"<b>{content['title']}</b>\n\n{body}"
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()

    async def _send_aggregated_zulip(self, config: dict, content: dict):
        site = config.get("site")
        api_key = config.get("api_key")
        email = config.get("email")
        stream = config.get("stream", "general")
        topic = config.get("topic", "TagWatcher Updates")
        if not all([site, api_key, email]):
            raise ValueError("Zulip config missing required fields")
        err = validate_webhook_url(site)
        if err:
            raise ValueError(f"Zulip site URL invalid: {err}")

        ctx = self._context_lines(content)
        items = "\n".join(
            f"- [{l['name']}]({l['url']}): `{l['current']}` → `{l['latest']}`{l['release']}"
            for l in content["lines"]
        )
        body = f"{ctx}\n\n{items}" if ctx else items
        message = f"**{content['title']}**\n\n{body}"
        resp = await self._client.post(
            f"{site.rstrip('/')}/api/v1/messages",
            data={"type": "stream", "to": stream, "topic": topic, "content": message},
            auth=(email, api_key),
        )
        resp.raise_for_status()

    async def _send_aggregated_mattermost(self, config: dict, content: dict):
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Mattermost config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Mattermost webhook URL invalid: {err}")

        ctx = self._context_lines(content)
        items = "\n".join(
            f"- **[{l['name']}]({l['url']})** `{l['current']}` → `{l['latest']}`{l['release']}"
            for l in content["lines"]
        )
        body = f"{ctx}\n\n{items}" if ctx else items
        text = f"#### {content['title']}\n{body}"
        payload: dict[str, Any] = {"text": text, "username": config.get("username", "TagWatcher")}
        if config.get("channel"):
            payload["channel"] = config["channel"]
        resp = await self._client.post(webhook_url, json=payload)
        resp.raise_for_status()

    async def _send_aggregated_teams(self, config: dict, content: dict):
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Teams config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Teams webhook URL invalid: {err}")

        ctx_facts = []
        if content.get("space_name"):
            ctx_facts.append({"name": "Space", "value": content["space_name"]})
        if content.get("host_name"):
            ctx_facts.append({"name": "Host", "value": content["host_name"]})
        if content.get("check_time"):
            ctx_facts.append({"name": "시간", "value": content["check_time"]})

        facts = ctx_facts + [
            {"name": l["name"], "value": f"`{l['current']}` → `{l['latest']}`{l['release']}"}
            for l in content["lines"]
        ]
        payload = {
            _TEAMS_MSG_CARD_TYPE: "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": "F59E0B",
            "summary": content["title"],
            "sections": [{"activityTitle": content["title"], "facts": facts}],
            "potentialAction": [
                {_TEAMS_MSG_CARD_TYPE: "OpenUri", "name": _VIEW_IN_TAGWATCHER,
                 "targets": [{"os": "default", "uri": content["lines"][0]["url"]}]}
            ] if content["lines"] else [],
        }
        resp = await self._client.post(webhook_url, json=payload)
        resp.raise_for_status()

    async def send_test_notification(self, channel: NotificationChannel, app_url: str):
        """Send a test notification to verify configuration."""
        content = {
            "container_name": "test-container",
            "image": "nginx",
            "current_version": "1.25.0",
            "new_version": "1.26.0",
            "release_date": "2024-01-01",
            "container_url": f"{app_url.rstrip('/')}/containers/test",
            "title": "TagWatcher Test Notification",
            "summary": "This is a test notification from TagWatcher.",
            "host_name": "",
            "space_name": "",
            "check_time": "",
        }

        await self._dispatch(channel, content, aggregated=False)

    async def send_slack(self, config: dict, content: dict):
        """Send a Slack Block Kit message with action button."""
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Slack config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Slack webhook URL invalid: {err}")

        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": content["title"], "emoji": True},
            },
        ]
        ctx = self._context_lines(content)
        if ctx:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": ctx}]})
        fields = []
        if content.get("namespace"):
            fields.append({"type": "mrkdwn", "text": f"*Namespace:*\n{content['namespace']}"})
        fields += [
            {"type": "mrkdwn", "text": f"*Container:*\n{content['container_name']}"},
            {"type": "mrkdwn", "text": f"*Image:*\n{content['image']}"},
            {"type": "mrkdwn", "text": f"*Current Version:*\n`{content['current_version']}`"},
            {"type": "mrkdwn", "text": f"*New Version:*\n`{content['new_version']}`"},
        ]
        blocks.append({"type": "section", "fields": fields})

        if content.get("release_date"):
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Release Date:* {content['release_date']}"},
            })

        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": _VIEW_IN_TAGWATCHER},
                    "url": content["container_url"],
                    "style": "primary",
                }
            ],
        })

        payload = {"blocks": blocks}
        resp = await self._client.post(webhook_url, json=payload)
        resp.raise_for_status()
        logger.info(f"Slack notification sent for {content['container_name']}")

    async def send_discord(self, config: dict, content: dict):
        """Send a Discord embed message via webhook."""
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Discord config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Discord webhook URL invalid: {err}")

        ctx_fields = []
        if content.get("space_name"):
            ctx_fields.append({"name": "Space", "value": content["space_name"], "inline": True})
        if content.get("host_name"):
            ctx_fields.append({"name": "Host", "value": content["host_name"], "inline": True})
        if content.get("check_time"):
            ctx_fields.append({"name": "시간", "value": content["check_time"], "inline": True})

        ns_fields = []
        if content.get("namespace"):
            ns_fields.append({"name": "Namespace", "value": content["namespace"], "inline": True})

        embed = {
            "title": content["title"],
            "color": 0xF59E0B,  # amber
            "fields": ctx_fields + ns_fields + [
                {"name": "Container", "value": content["container_name"], "inline": True},
                {"name": "Image", "value": content["image"], "inline": True},
                {"name": "Current Version", "value": f"`{content['current_version']}`", "inline": False},
                {"name": "New Version", "value": f"`{content['new_version']}`", "inline": False},
            ],
            "url": content["container_url"],
        }

        if content.get("release_date"):
            embed["fields"].append(
                {"name": "Release Date", "value": content["release_date"], "inline": True}
            )

        embed["footer"] = {"text": "TagWatcher | View in TagWatcher: " + content["container_url"]}

        payload = {
            "embeds": [embed],
            "components": [
                {
                    "type": 1,
                    "components": [
                        {
                            "type": 2,
                            "style": 5,  # LINK style
                            "label": _VIEW_IN_TAGWATCHER,
                            "url": content["container_url"],
                        }
                    ],
                }
            ],
        }

        resp = await self._client.post(webhook_url, json=payload)
        resp.raise_for_status()
        logger.info(f"Discord notification sent for {content['container_name']}")

    async def send_telegram(self, config: dict, content: dict):
        """Send a Telegram message with inline keyboard button."""
        bot_token = config.get("bot_token")
        chat_id = config.get("chat_id")
        if not bot_token or not chat_id:
            raise ValueError("Telegram config missing 'bot_token' or 'chat_id'")

        ctx = self._context_lines(content)
        text = f"*{content['title']}*\n\n"
        if ctx:
            text += ctx + "\n\n"
        if content.get("namespace"):
            text += f"Namespace: `{content['namespace']}`\n"
        text += (
            f"Container: `{content['container_name']}`\n"
            f"Image: `{content['image']}`\n"
            f"Current: `{content['current_version']}`\n"
            f"New: `{content['new_version']}`"
        )
        if content.get("release_date"):
            text += f"\nReleased: {content['release_date']}"

        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": _VIEW_IN_TAGWATCHER, "url": content["container_url"]}]
                ]
            },
        }

        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        logger.info(f"Telegram notification sent for {content['container_name']}")

    async def send_zulip(self, config: dict, content: dict):
        """Send a Zulip message via the Zulip REST API."""
        site = config.get("site")  # e.g. https://yourorg.zulipchat.com
        api_key = config.get("api_key")
        email = config.get("email")
        stream = config.get("stream", "general")
        topic = config.get("topic", "TagWatcher Updates")

        if not all([site, api_key, email]):
            raise ValueError("Zulip config missing 'site', 'api_key', or 'email'")
        err = validate_webhook_url(site)
        if err:
            raise ValueError(f"Zulip site URL invalid: {err}")

        ctx = self._context_lines(content)
        message = f"**{content['title']}**\n\n"
        if ctx:
            message += ctx + "\n\n"
        if content.get("namespace"):
            message += f"- Namespace: `{content['namespace']}`\n"
        message += (
            f"- Container: `{content['container_name']}`\n"
            f"- Image: `{content['image']}`\n"
            f"- Current: `{content['current_version']}`\n"
            f"- New: `{content['new_version']}`"
        )
        if content.get("release_date"):
            message += f"\n- Released: {content['release_date']}"
        message += f"\n\n[View in TagWatcher]({content['container_url']})"

        payload = {
            "type": "stream",
            "to": stream,
            "topic": topic,
            "content": message,
        }

        resp = await self._client.post(
            f"{site.rstrip('/')}/api/v1/messages",
            data=payload,
            auth=(email, api_key),
        )
        resp.raise_for_status()
        logger.info(f"Zulip notification sent for {content['container_name']}")

    async def send_mattermost(self, config: dict, content: dict):
        """Send a Mattermost incoming webhook message (similar to Slack format)."""
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Mattermost config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Mattermost webhook URL invalid: {err}")

        channel = config.get("channel", "")
        username = config.get("username", "TagWatcher")

        ctx = self._context_lines(content)
        text = f"#### {content['title']}\n"
        if ctx:
            text += ctx + "\n\n"
        text += (
            f"| Field | Value |\n"
            f"|-------|-------|\n"
            f"| Container | `{content['container_name']}` |\n"
            f"| Image | `{content['image']}` |\n"
            f"| Current | `{content['current_version']}` |\n"
            f"| New | `{content['new_version']}` |"
        )
        if content.get("release_date"):
            text += f"\n| Released | {content['release_date']} |"
        text += f"\n\n[View in TagWatcher]({content['container_url']})"

        payload: dict[str, Any] = {
            "text": text,
            "username": username,
        }
        if channel:
            payload["channel"] = channel

        resp = await self._client.post(webhook_url, json=payload)
        resp.raise_for_status()
        logger.info(f"Mattermost notification sent for {content['container_name']}")

    async def send_teams(self, config: dict, content: dict):
        """Send a Microsoft Teams MessageCard via incoming webhook."""
        webhook_url = config.get("webhook_url")
        if not webhook_url:
            raise ValueError("Teams config missing 'webhook_url'")
        err = validate_webhook_url(webhook_url)
        if err:
            raise ValueError(f"Teams webhook URL invalid: {err}")

        ctx_facts = []
        if content.get("space_name"):
            ctx_facts.append({"name": "Space", "value": content["space_name"]})
        if content.get("host_name"):
            ctx_facts.append({"name": "Host", "value": content["host_name"]})
        if content.get("check_time"):
            ctx_facts.append({"name": "시간", "value": content["check_time"]})

        facts = ctx_facts + [
            {"name": "Container", "value": content["container_name"]},
            {"name": "Image", "value": content["image"]},
            {"name": "Current Version", "value": content["current_version"]},
            {"name": "New Version", "value": content["new_version"]},
        ]
        if content.get("release_date"):
            facts.append({"name": "Release Date", "value": content["release_date"]})

        payload = {
            _TEAMS_MSG_CARD_TYPE: "MessageCard",
            "@context": "https://schema.org/extensions",
            "themeColor": "F59E0B",
            "summary": content["title"],
            "sections": [
                {
                    "activityTitle": content["title"],
                    "activitySubtitle": content["image"],
                    "facts": facts,
                }
            ],
            "potentialAction": [
                {
                    _TEAMS_MSG_CARD_TYPE: "OpenUri",
                    "name": _VIEW_IN_TAGWATCHER,
                    "targets": [{"os": "default", "uri": content["container_url"]}],
                }
            ],
        }

        resp = await self._client.post(webhook_url, json=payload)
        resp.raise_for_status()
        logger.info(f"Teams notification sent for {content['container_name']}")
