"""Display masking for CNIC and phone numbers (Milestone 5 foundation).

No API response returns CNIC or phone today (see
docs/architecture/TRUST_AND_SAFETY.md, privacy section). These helpers are
the only sanctioned way to show either value to anyone other than its owner,
e.g. a future admin user lookup. They fail closed: a value that is not a
recognisable CNIC / Pakistani mobile (legacy rows were stored unvalidated)
is masked completely instead of being partially echoed.

CNIC masking hides the first five digits (they encode the issuing region)
and the final digit (it encodes gender: odd male, even female), and shows
only the last three digits of the middle serial block, which is enough for a
human to tell two records apart on a call or review screen.
"""

import re

CNIC_FULLY_MASKED = "*****-*******-*"
PHONE_FULLY_MASKED = "+92 *** *** ****"

_CNIC_DIGITS = re.compile(r"[0-9]{13}")
_CNIC_SHAPE = re.compile(r"[0-9]{5}-?[0-9]{7}-?[0-9]")
_PK_MOBILE = re.compile(r"(?:\+?92|0)(3[0-9]{9})")


def mask_cnic(value: str | None) -> str | None:
    """``12345-1234567-1`` -> ``*****-****567-*``. Unrecognised -> fully masked."""
    if value is None:
        return None
    candidate = value.strip()
    if not _CNIC_SHAPE.fullmatch(candidate):
        return CNIC_FULLY_MASKED
    digits = candidate.replace("-", "")
    if not _CNIC_DIGITS.fullmatch(digits):
        return CNIC_FULLY_MASKED
    return f"*****-****{digits[9:12]}-*"


def mask_phone(value: str | None) -> str | None:
    """``+923001234567`` -> ``+92 3** *** **67``. Unrecognised -> fully masked."""
    if value is None:
        return None
    compact = re.sub(r"[ -]", "", value.strip())
    match = _PK_MOBILE.fullmatch(compact)
    if match is None:
        return PHONE_FULLY_MASKED
    return f"+92 3** *** **{match.group(1)[-2:]}"


EMAIL_FULLY_MASKED = "***@***"


def mask_email(value: str | None) -> str | None:
    """``ayesha.khan@example.com`` -> ``a***@example.com``. Unrecognised -> fully masked.

    The domain stays visible (support needs to tell providers apart); the
    local part shows only its first character.
    """
    if value is None:
        return None
    local, at, domain = value.strip().rpartition("@")
    if not at or not local or not domain or "@" in local or not local[0].isascii():
        return EMAIL_FULLY_MASKED
    return f"{local[0]}***@{domain}"
