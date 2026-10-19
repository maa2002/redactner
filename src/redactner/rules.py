"""Pattern recognisers with checksum validation (modification M5).

Structured identifiers are defined by their format, so a regular expression plus a validator
(Luhn, mod-97, the Australian TFN/ABN/Medicare check digits, libphonenumber, ``ipaddress``) is
near-perfectly precise and fully explainable. Validated matches get score 1.0; weaker,
context-gated matches get lower scores so the fusion step can rank them below the model.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable

import phonenumbers
from stdnum import iban as std_iban
from stdnum import luhn
from stdnum.au import abn as au_abn
from stdnum.au import tfn as au_tfn

from .types import Span

EMAIL_RE = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*\.[A-Za-z]{2,24}(?![\w-])"
)
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>\"'\[\]{}|\\^`]{2,2000}", re.IGNORECASE)
IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
IPV6_RE = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
MAC_RE = re.compile(r"(?<![\w:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![\w:-])")
CARD_RE = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")
TFN_RE = re.compile(r"(?<![\d-])\d{3}[ -]?\d{3}[ -]?\d{2,3}(?![\d-])")
ABN_RE = re.compile(r"(?<![\d-])\d{2} ?\d{3} ?\d{3} ?\d{3}(?![\d-])")
MEDICARE_RE = re.compile(r"(?<![\d-])[2-6]\d{3} ?\d{5} ?\d(?:[ -/]?\d)?(?![\d-])")
SSN_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}(?![\d-])")
BSB_RE = re.compile(r"(?<![\d-])\d{3}-\d{3}(?![\d-])")
PASSPORT_RE = re.compile(r"\b[A-Z]{1,2}\d{7}\b")
# Australian-format numbers that libphonenumber considers possible but not assigned (recall-first fallback)
AU_PHONE_RE = re.compile(r"(?<![\d+])(?:\+61[ -]?\(?0?\)?|0)[2-478](?:[ -]?\d){8}(?![\d-])")

CONTEXT_WINDOW = 48
CONTEXT = {
    "tfn": re.compile(r"\b(tfn|tax\s*file)", re.IGNORECASE),
    "medicare": re.compile(r"\bmedicare\b", re.IGNORECASE),
    "bsb": re.compile(r"\bbsb\b", re.IGNORECASE),
    "card": re.compile(r"\b(card|visa|mastercard|amex|credit|debit)\b", re.IGNORECASE),
    "passport": re.compile(r"\bpassport\b", re.IGNORECASE),
    "abn": re.compile(r"\babn\b", re.IGNORECASE),
}
PHONE_REGIONS = ("AU", "US", "GB")


def digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def has_context(text: str, start: int, key: str) -> bool:
    return bool(CONTEXT[key].search(text[max(0, start - CONTEXT_WINDOW):start]))


def medicare_valid(number: str) -> bool:
    """Australian Medicare card number: 10 digits (+ optional IRN), check digit at position 9."""
    d = digits(number)
    if len(d) not in (10, 11) or d[0] not in "23456":
        return False
    weights = (1, 3, 7, 9, 1, 3, 7, 9)
    return sum(int(x) * w for x, w in zip(d[:8], weights)) % 10 == int(d[8])


class RuleDetector:
    """Regex + validator recognisers for structured PII."""

    name = "rules"

    def detect(self, text: str) -> list[Span]:
        spans: list[Span] = []
        spans += self._emails(text)
        spans += self._urls(text)
        spans += self._ips(text)
        spans += self._phones(text)
        spans += self._numbers(text)
        return spans

    # -- recognisers -----------------------------------------------------------------------
    @staticmethod
    def _emails(text: str) -> list[Span]:
        return [Span(m.start(), m.end(), "EMAIL", 1.0, "rule") for m in EMAIL_RE.finditer(text)]

    @staticmethod
    def _urls(text: str) -> list[Span]:
        out = []
        for m in URL_RE.finditer(text):
            end = m.end()
            while end > m.start() and text[end - 1] in ".,;:!?)'\"":
                end -= 1
            if end - m.start() > 4:
                out.append(Span(m.start(), end, "URL", 0.95, "rule"))
        return out

    @staticmethod
    def _ips(text: str) -> list[Span]:
        out = []
        for regex in (IPV4_RE, IPV6_RE):
            for m in regex.finditer(text):
                candidate = m.group(0)
                if regex is IPV6_RE and candidate.count(":") < 2:
                    continue
                try:
                    ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                out.append(Span(m.start(), m.end(), "IP_ADDRESS", 1.0, "rule"))
        out += [Span(m.start(), m.end(), "IP_ADDRESS", 0.95, "rule") for m in MAC_RE.finditer(text)]
        return out

    @staticmethod
    def _phones(text: str) -> list[Span]:
        found: dict[tuple[int, int], Span] = {}
        for region in PHONE_REGIONS:
            try:
                matcher = phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID)
                for match in matcher:
                    key = (match.start, match.end)
                    if key not in found and len(digits(match.raw_string)) >= 8:
                        found[key] = Span(match.start, match.end, "PHONE", 0.97, "rule")
            except Exception:  # noqa: BLE001 - libphonenumber can raise on pathological input
                continue
        for m in AU_PHONE_RE.finditer(text):
            if not any(s.start <= m.start() < s.end or m.start() <= s.start < m.end() for s in found.values()):
                found[(m.start(), m.end())] = Span(m.start(), m.end(), "PHONE", 0.85, "rule")
        return list(found.values())

    @staticmethod
    def _numbers(text: str) -> list[Span]:
        out: list[Span] = []
        for m in CARD_RE.finditer(text):
            d = digits(m.group(0))
            if 13 <= len(d) <= 19 and len(set(d)) > 1 and luhn.is_valid(d):
                grouped = bool(re.fullmatch(r"\d{4}([ -]\d{4}){2,3}(?:[ -]\d{1,3})?", m.group(0)))
                score = 1.0 if grouped or has_context(text, m.start(), "card") else 0.8
                out.append(Span(m.start(), m.end(), "FINANCIAL", score, "rule"))
        for m in IBAN_RE.finditer(text):
            if std_iban.is_valid(m.group(0)):
                out.append(Span(m.start(), m.end(), "FINANCIAL", 1.0, "rule"))
        for m in TFN_RE.finditer(text):
            d = digits(m.group(0))
            if len(d) in (8, 9) and au_tfn.is_valid(d):
                if has_context(text, m.start(), "tfn") or re.fullmatch(r"\d{3} \d{3} \d{3}", m.group(0)):
                    out.append(Span(m.start(), m.end(), "ID_NUMBER", 1.0, "rule"))
        for m in ABN_RE.finditer(text):
            if au_abn.is_valid(digits(m.group(0))):
                score = 1.0 if has_context(text, m.start(), "abn") else 0.85
                out.append(Span(m.start(), m.end(), "ID_NUMBER", score, "rule"))
        for m in MEDICARE_RE.finditer(text):
            if medicare_valid(m.group(0)) and (has_context(text, m.start(), "medicare")
                                                or re.fullmatch(r"\d{4} \d{5} \d(?: \d)?", m.group(0))):
                out.append(Span(m.start(), m.end(), "ID_NUMBER", 1.0, "rule"))
        for m in SSN_RE.finditer(text):
            out.append(Span(m.start(), m.end(), "ID_NUMBER", 0.95, "rule"))
        for m in BSB_RE.finditer(text):
            if has_context(text, m.start(), "bsb"):
                out.append(Span(m.start(), m.end(), "FINANCIAL", 0.95, "rule"))
        for m in PASSPORT_RE.finditer(text):
            if has_context(text, m.start(), "passport"):
                out.append(Span(m.start(), m.end(), "ID_NUMBER", 0.9, "rule"))
        return out


def term_spans(text: str, terms: Iterable[str], label: str = "CUSTOM") -> list[Span]:
    """Case-insensitive, word-bounded occurrences of user deny-list terms."""
    out = []
    for term in {t.strip() for t in terms if t and t.strip()}:
        pattern = re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)
        out += [Span(m.start(), m.end(), label, 1.0, "deny") for m in pattern.finditer(text)]
    return out
