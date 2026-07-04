"""
Slack client helpers — DMs and message posting.
"""
from typing import Optional

from slack_sdk.web.async_client import AsyncWebClient

from app.config import settings

_client: Optional[AsyncWebClient] = None


def get_slack_client() -> AsyncWebClient:
    global _client
    if _client is None:
        _client = AsyncWebClient(token=settings.slack_bot_token)
    return _client


async def send_dm(slack_user_id: str, text: str, blocks=None) -> Optional[str]:
    """Open a DM with a user and post a message. Returns the message ts or None on failure."""
    if not slack_user_id:
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
