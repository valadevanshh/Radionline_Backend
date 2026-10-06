"""Canonical billing pricing (INR). Source of truth for sign-off charges.

The constants below are the DEFAULT rate card. A centre can have its own rates
(table center_pricing, edited by the Super Admin); billing.rates_for_center()
returns that centre's Rates, or DEFAULT_RATES when it has none. Rates apply at
sign-off only: line items keep the amount they were billed at.
"""
from dataclasses import dataclass

# Center charges (diagnostic center is billed)
CENTER_FIRST_STUDY_INR = 30
CENTER_ADDITIONAL_STUDY_INR = 15

# Doctor payouts (radiologist is paid)
DOCTOR_FIRST_STUDY_INR = 20
DOCTOR_ADDITIONAL_STUDY_INR = 10

CURRENCY = "INR"

# Study modalities accepted on upload / studies (not template category labels)
ALLOWED_STUDY_MODALITIES = (
    "X-Ray",
    "CT",
    "MRI",
    "Sonography",
    "Blood Report",
)

# Last-resort default when client omits modality (legacy callers). Prefer explicit client value.
DEFAULT_STUDY_MODALITY = "X-Ray"


def center_amount_for_study_index(index_one_based: int) -> int:
    """index_one_based: 1 = first study in the case, 2+ = additional."""
    if index_one_based <= 1:
        return CENTER_FIRST_STUDY_INR
    return CENTER_ADDITIONAL_STUDY_INR


def doctor_amount_for_study_index(index_one_based: int) -> int:
    if index_one_based <= 1:
        return DOCTOR_FIRST_STUDY_INR
    return DOCTOR_ADDITIONAL_STUDY_INR


def center_total_for_study_count(n: int) -> int:
    if n <= 0:
        return 0
    return CENTER_FIRST_STUDY_INR + CENTER_ADDITIONAL_STUDY_INR * (n - 1)


def doctor_total_for_study_count(n: int) -> int:
    if n <= 0:
        return 0
    return DOCTOR_FIRST_STUDY_INR + DOCTOR_ADDITIONAL_STUDY_INR * (n - 1)


# Upper bound for one per-study rate entered by the Super Admin (typo guard).
MAX_RATE_INR = 100000


@dataclass(frozen=True)
class Rates:
    """One centre's rate card (INR, whole rupees)."""
    center_first: int = CENTER_FIRST_STUDY_INR
    center_additional: int = CENTER_ADDITIONAL_STUDY_INR
    doctor_first: int = DOCTOR_FIRST_STUDY_INR
    doctor_additional: int = DOCTOR_ADDITIONAL_STUDY_INR

    def center_amount(self, index_one_based: int) -> int:
        return self.center_first if index_one_based <= 1 else self.center_additional

    def doctor_amount(self, index_one_based: int) -> int:
        return self.doctor_first if index_one_based <= 1 else self.doctor_additional

    def center_total(self, n: int) -> int:
        return 0 if n <= 0 else self.center_first + self.center_additional * (n - 1)

    def doctor_total(self, n: int) -> int:
        return 0 if n <= 0 else self.doctor_first + self.doctor_additional * (n - 1)

    @property
    def is_default(self) -> bool:
        return self == DEFAULT_RATES

    def as_dict(self) -> dict:
        return {
            "center": {"firstStudy": self.center_first, "additionalStudy": self.center_additional},
            "doctor": {"firstStudy": self.doctor_first, "additionalStudy": self.doctor_additional},
        }


DEFAULT_RATES = Rates()


def pricing_public_dict(rates: "Rates | None" = None) -> dict:
    rates = rates or DEFAULT_RATES
    return {
        "currency": CURRENCY,
        **rates.as_dict(),
        "isDefault": rates.is_default,
        "default": DEFAULT_RATES.as_dict(),
        "allowedModalities": list(ALLOWED_STUDY_MODALITIES),
    }
