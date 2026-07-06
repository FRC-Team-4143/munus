"""
Slack client helpers — DMs and message posting.
"""
import logging
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings

log = logging.getLogger(__name__)

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


async def open_modal(trigger_id: str, view: dict) -> bool:
    """Open a Slack modal for an interaction. Returns True on success.

    `trigger_id` is short-lived (~3s), so call this promptly from the interaction handler.
    """
    from slack_sdk.errors import SlackApiError

    client = get_slack_client()
    try:
        await client.views_open(trigger_id=trigger_id, view=view)
        return True
    except SlackApiError as e:
        # Surface Slack's actual reason (e.g. invalid_arguments, expired_trigger_id,
        # missing_scope, not_authed) so modal failures aren't silent.
        log.error("views.open failed: %s", e.response.get("error", e))
        return False
    except Exception as e:
        log.error("views.open failed: %s", e)
        return False


async def send_dm(slack_user_id: str, text: str, blocks=None, automated: bool = False) -> Optional[str]:
    """Open a DM with a user and post a message. Returns the message ts or None on failure.
    If automated=True, skips sending when updates_enabled is false."""
    if not slack_user_id:
        return None
    if automated and not settings.updates_enabled:
        return None
    client = get_slack_client()
    try:
        conv = await client.conversations_open(users=slack_user_id)
        channel_id = conv["channel"]["id"]
        result = await client.chat_postMessage(
            channel=channel_id,
            text=text,
            blocks=blocks,
        )
        return result["ts"]
    except Exception:
        return None


async def post_to_channel(channel_id: str, text: str, blocks=None, automated: bool = True) -> Optional[str]:
    """Post a message to a channel. Returns the message ts or None on failure.
    If automated=True, skips sending when updates_enabled is false. The bot must be a
    member of the channel."""
    if not channel_id:
        return None
    if automated and not settings.updates_enabled:
        return None
    client = get_slack_client()
    try:
        result = await client.chat_postMessage(channel=channel_id, text=text, blocks=blocks)
        return result["ts"]
    except Exception:
        return None
