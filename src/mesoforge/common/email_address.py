"""Plain-mailbox identity shared by configured recipients and SMTP delivery."""

from __future__ import annotations

import re


def address(value: str) -> str:
    """One ASCII mailbox; DNS domain is case-insensitive, local part is preserved."""
    if len(value) > 254 or not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", value
    ):
        raise ValueError("Expected one plain email address")
    local, domain = value.rsplit("@", 1)
    if (
        len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or any(
            not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in domain.split(".")
        )
    ):
        raise ValueError("Expected one plain email address")
    return f"{local}@{domain.lower()}"
