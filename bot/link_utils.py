"""Shared helpers for recognizing Telegram message links across domain aliases.

Telegram links can appear under multiple interchangeable hostnames — the
canonical `t.me`, plus the legacy/alias domains `telegram.me` and
`telegram.dog`. All of them route to the same content in Telegram clients.

Every link parser in the bot used to hardcode `t.me` only, which silently
broke links whenever a user (or an outage on Telegram's side) caused
`telegram.me`/`telegram.dog` links to be shared instead. This module gives a
single place to recognize any alias and normalize it to `t.me` before the
existing `t.me`-shaped regexes run, so only one spot needs to change if
Telegram ever adds/retires an alias domain.
"""

import re

# Hostnames Telegram treats as equivalent for `t.me`-style deep links.
TG_LINK_HOSTS = ("t.me", "telegram.me", "telegram.dog")

# Matches `https://t.me/`, `http://telegram.me/`, etc. Use for filters that
# just need to detect "is this a Telegram link" (e.g. message filters).
TG_LINK_HOST_RE = r"https?://(?:{})/".format(
    "|".join(re.escape(host) for host in TG_LINK_HOSTS)
)

_DOMAIN_RE = re.compile(
    r"^(https?://)(?:{})/".format("|".join(re.escape(host) for host in TG_LINK_HOSTS)),
    re.IGNORECASE,
)


def normalize_telegram_link(link: str) -> str:
    """Rewrite any recognized Telegram link alias domain to the canonical `t.me`.

    Leaves the rest of the link (path, query string) untouched. Non-Telegram
    links, or links that don't start with a recognized host, are returned
    unchanged so existing "did this match?" checks downstream still fail
    correctly for unrelated text.
    """
    return _DOMAIN_RE.sub(r"\1t.me/", link)
