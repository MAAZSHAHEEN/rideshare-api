"""Server-side account capabilities, independent of the selected UI mode.

Legacy passenger/driver roles remain stored and signed for compatibility. Both
are ordinary accounts; admin is deliberately excluded. A future persisted driver
eligibility/verification policy belongs here and in the account response, not in
a client-selected mode or a replacement JWT role. No verification is implied today.
"""

from typing import Literal

from models import UserRole

AccountMode = Literal["passenger", "driver"]


def allowed_modes(role: UserRole) -> list[AccountMode]:
    """Use the authenticated database role, never a request's selected mode."""
    if role in (UserRole.passenger, UserRole.driver):
        return ["passenger", "driver"]
    return []
