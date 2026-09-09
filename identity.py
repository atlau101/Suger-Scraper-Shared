#!/usr/bin/env python3
"""The identity gate — shared across every cloud's partner lookup.

A partner directory search returns whatever its relevance ranking likes.
Searching "Palo Alto Networks" in Microsoft's directory returns
"PALO IT SINGAPORE PTE. LTD." and "AI Inversiones Palo Alto II SAC" alongside
the real company. Result rank is NOT evidence of identity.

This module encodes the one rule that keeps "confirmed" meaning the same thing
on every cloud: confirm only on an exact domain match or an exact canonical
name match, and only when exactly one candidate qualifies. Everything else is
a review or a not-found. Forking this per cloud is how the word "confirmed"
quietly stops meaning anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from normalize import canonical_company_name, normalize_domain, review_company_name

# Lookup states. Shared vocabulary — do not add a cloud-specific state.
CONFIRMED = "confirmed"
MATCH_REVIEW = "match_review"
NOT_FOUND = "not_found_in_searched_source"
LOOKUP_ERROR = "lookup_error"


@dataclass
class SearchCandidate:
    """One row returned by a partner directory search."""

    displayed_name: str
    official_domain: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


def dedupe_candidates(candidates: list[SearchCandidate]) -> list[SearchCandidate]:
    seen: set[tuple[str, str]] = set()
    out: list[SearchCandidate] = []
    for candidate in candidates:
        key = (
            canonical_company_name(candidate.displayed_name),
            normalize_domain(candidate.official_domain),
        )
        if key not in seen:
            seen.add(key)
            out.append(candidate)
    return out


def select_candidate(
    vendor_name: str,
    website_domain: str,
    candidates: list[SearchCandidate],
) -> tuple[str, str, SearchCandidate | None]:
    """Return (state, match_method, the one safe candidate or None).

    Exact domain beats exact name. Fuzzy never auto-merges — it can only
    raise a review. `not_found_in_searched_source` means this directory did
    not return them; it never means "not a partner".
    """
    canonical = canonical_company_name(vendor_name)
    domain = normalize_domain(website_domain)

    exact_domain = [
        c for c in candidates
        if domain and normalize_domain(c.official_domain) == domain
    ]
    exact_name = [
        c for c in candidates
        if canonical and canonical_company_name(c.displayed_name) == canonical
    ]
    matches = exact_domain or exact_name

    if len(matches) == 1:
        return CONFIRMED, "exact_domain" if exact_domain else "exact_name", matches[0]
    if len(matches) > 1:
        return MATCH_REVIEW, "multiple_exact_candidates", None

    review_key = review_company_name(vendor_name)
    aliases = [
        c for c in candidates
        if review_key and review_company_name(c.displayed_name) == review_key
    ]
    if aliases:
        return MATCH_REVIEW, "alias_or_parent_review", None
    return NOT_FOUND, "none", None


# Countries that are in scope for outbound. Canada is in scope and is never
# `foreign` — see AGENTS.md. `foreign` is an operational DQ (calling windows,
# contracting law), not a judgement about fit.
DOMESTIC_COUNTRIES = {"US", "USA", "UNITED STATES", "CA", "CANADA"}


def geo_status(country_code: str | None) -> str:
    """Return 'domestic' | 'foreign' | 'unknown' from an ISO country code.

    Geography is a SEPARATE COLUMN, never a disqualification. A vendor that
    reads as non-domestic is ranked down, not DQ'd. There is no `foreign`
    dq_reason in any cloud; a validator that receives one must demote the row,
    not abort the run. Canada is domestic.

    This value is provisional evidence about calling hours and contracting law,
    not a judgement about product fit. Treat it as positive evidence only: a
    'foreign' or 'unknown' result is NOT evidence a company lacks a North
    American presence, because partner directories simply have no US profile
    for many genuinely-US companies.

    Canonical rules: Marketplaces/ARCHITECTURE.md, 'The shared judgement
    vocabulary'. Note that GCP's validator spells 'domestic' as 'in_scope';
    the two are the same state and the mismatch is documented there.
    """
    if not country_code:
        return "unknown"
    return "domestic" if str(country_code).strip().upper() in DOMESTIC_COUNTRIES else "foreign"
