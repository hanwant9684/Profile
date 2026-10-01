"""Helpers for treating expected Telegram status-message races as no-ops."""


_STALE_STATUS_MARKERS = (
    "MESSAGE_ID_INVALID",
    "MESSAGE_NOT_MODIFIED",
    "MESSAGE_DELETED",
    "MESSAGE_DELETE_FORBIDDEN",
    "MESSAGE_CANT_BE_EDITED",
    "MESSAGE_EDIT_TIME_EXPIRED",
    "MESSAGE_TO_EDIT_NOT_FOUND",
)


def is_stale_status_error(error: BaseException) -> bool:
    """Return whether Telegram has made a status message impossible to edit."""
    text = str(error).upper()
    return any(marker in text for marker in _STALE_STATUS_MARKERS)