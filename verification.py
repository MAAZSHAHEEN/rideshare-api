"""Driver verification status: the honest, code-only foundation (Milestone 5).

RideShare does not verify drivers today. There is no document upload, no
CNIC check, no licence or vehicle check and no review workflow, so every
driver is ``not_verified``. The target state machine
(not_verified -> submitted -> under_review -> approved / rejected, plus
suspended) is designed in docs/architecture/TRUST_AND_SAFETY.md and needs a
``driver_profiles`` table that does not exist yet.

Rules this module encodes:
- Nothing in the codebase may report a driver as verified until a persisted
  review decision exists. ``driver_verification_status`` therefore has no
  input that can make it return anything but ``not_verified``.
- A database boolean or a self-declared field is never verification.
- The status is not yet exposed in API responses: adding it to
  ``RideResponse`` would change the pinned ride response contract
  (tests/test_frontend_reads.py asserts its exact key set) while the
  passenger/driver UI is being built on it. The UI shows the fixed label
  below for every driver instead.
"""

import enum

from models import User


class DriverVerificationStatus(str, enum.Enum):
    not_verified = "not_verified"
    submitted = "submitted"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    suspended = "suspended"


DRIVER_NOT_VERIFIED_LABEL = "Driver documents not verified"


def driver_verification_status(driver: User) -> DriverVerificationStatus:
    """Always ``not_verified`` until verification is actually built."""
    return DriverVerificationStatus.not_verified


def is_verified_driver(driver: User) -> bool:
    return driver_verification_status(driver) is DriverVerificationStatus.approved
