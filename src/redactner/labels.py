"""The 13-type PII taxonomy used across training, evaluation and the app."""

from __future__ import annotations

LABELS: list[str] = [
    "PERSON",
    "EMAIL",
    "PHONE",
    "ADDRESS",
    "LOCATION",
    "DATE",
    "ID_NUMBER",
    "FINANCIAL",
    "USERNAME",
    "CREDENTIAL",
    "IP_ADDRESS",
    "URL",
    "ORGANIZATION",
]

# BIO tag set in a fixed order: index 0 is "O", then B-/I- pairs per type.
BIO_LABELS: list[str] = ["O"] + [f"{prefix}-{label}" for label in LABELS for prefix in ("B", "I")]

DESCRIPTIONS: dict[str, str] = {
    "PERSON": "Names of people, including initials",
    "EMAIL": "E-mail addresses",
    "PHONE": "Phone, mobile and fax numbers",
    "ADDRESS": "Street addresses, unit/building numbers, postcodes",
    "LOCATION": "Cities, suburbs, states, coordinates",
    "DATE": "Dates of birth and other personal dates",
    "ID_NUMBER": "Government, tax, medical, employee, customer and vehicle identifiers",
    "FINANCIAL": "Card numbers, bank accounts, BSB, IBAN, SWIFT/BIC",
    "USERNAME": "User names and handles",
    "CREDENTIAL": "Passwords, PINs, CVVs, API keys",
    "IP_ADDRESS": "IPv4, IPv6 and MAC addresses",
    "URL": "Personal URLs and profile links",
    "ORGANIZATION": "Employers and company names (quasi-identifier)",
}

COLORS: dict[str, str] = {
    "PERSON": "#e6194b",
    "EMAIL": "#3cb44b",
    "PHONE": "#4363d8",
    "ADDRESS": "#f58231",
    "LOCATION": "#911eb4",
    "DATE": "#0e9fb8",
    "ID_NUMBER": "#f032e6",
    "FINANCIAL": "#7aa31c",
    "USERNAME": "#d6568f",
    "CREDENTIAL": "#469990",
    "IP_ADDRESS": "#8a6fd1",
    "URL": "#9a6324",
    "ORGANIZATION": "#808000",
}

PRESETS: dict[str, list[str]] = {
    "strict": [label for label in LABELS if label != "ORGANIZATION"],
    "direct": [
        "PERSON",
        "EMAIL",
        "PHONE",
        "ADDRESS",
        "ID_NUMBER",
        "FINANCIAL",
        "USERNAME",
        "CREDENTIAL",
        "IP_ADDRESS",
        "URL",
    ],
}
DEFAULT_PRESET = "strict"


def label_info() -> list[dict]:
    """Taxonomy metadata for the UI/API."""
    on = set(PRESETS[DEFAULT_PRESET])
    return [
        {
            "name": label,
            "color": COLORS[label],
            "description": DESCRIPTIONS[label],
            "default_on": label in on,
        }
        for label in LABELS
    ]
