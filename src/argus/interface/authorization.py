"""Authorization check — authorized chats only.

Prompt-injection/security: only authorized users can reach the brain and the commands.
"""

from __future__ import annotations

from collections.abc import Iterable


def is_authorized(chat_id: int, authorized: Iterable[int]) -> bool:
    """Return whether ``chat_id`` is in the authorized set."""
    return chat_id in set(authorized)
