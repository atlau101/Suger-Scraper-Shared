#!/usr/bin/env python3
"""Company-name and domain normalisation — shared across every cloud.

Promoted from GCP on Azure's arrival. Two near-identical copies previously
lived in gcp_marketplace.py and gcp_partner_status.py and had already drifted.
vendor_key is derived from canonical_company_name, so any divergence here
silently breaks cross-cloud joins: the same vendor would get two keys and a
company listed on both Azure and GCP would look like two companies.

Everything in this module is pure. No I/O, no cloud concepts.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlparse

# THE single suffix list for every cloud and every stage.
#
# This is the union of the two lists GCP previously kept in separate files.
# Reconciled 2026-09-07; both GCP modules now import from here.
#
# The divergence this replaces was real and had a measurable cost. The two
# copies agreed on only 22 entries:
#   gcp_marketplace.py   also had {ad, asa, kgaa, se, srl}
#   gcp_partner_status.py also had {aps, as, kft, kk, llp, lp, pty}
# so the same company canonicalised differently depending on which stage was
# looking at it — "Trafficguard Pty Ltd" was `trafficguard pty` in Stage 1 and
# `trafficguard` in Stage 2. 23 of GCP's 1,699 vendors (1.35%) were affected.
#
# The cost showed up as missed partner matches: with `pty` unstripped, Azure
# recorded "Lucid Labs Pty Ltd" and "BUI (Pty) Ltd" as
# not_found_in_searched_source when the directory lists them as "Lucid Labs"
# and "BUI". Sampling put the false-negative rate around 2 in 14 of the
# affected rows.
#
# BEFORE YOU CHANGE THIS SET: vendor_key is derived from it, so adding or
# removing an entry re-keys every affected company. Change it in one commit,
# re-run both clouds' qualify/export so the keys agree again, and expect
# already-published workbooks to carry the old keys.
#
LEGAL_SUFFIXES = {
    # agreed by both GCP modules
    "ab", "ag", "bv", "co", "company", "corp", "corporation", "gmbh",
    "inc", "incorporated", "kg", "limited", "llc", "ltd", "nv", "oy",
    "plc", "pte", "sa", "sarl", "sas", "spa",
    # previously only in gcp_marketplace.py
    "ad", "asa", "kgaa", "se", "srl",
    # previously only in gcp_partner_status.py
    "aps", "as", "kft", "kk", "llp", "lp", "pty",
    # added 2026-09-07 alongside the dotted-abbreviation collapse below.
    # These only become reachable once "S.L." folds to "sl", so the two
    # changes belong together.
    "sl", "sro", "doo", "dooel", "ou",
}

# Runs of single letters separated by dots are one abbreviation, not several
# words. Tokenising first splits "S.r.l." into s / r / l, so the suffix strip
# never sees "srl" and the name keeps a meaningless tail:
#
#   "BF PARTNERS SRL"     -> "bf partners"        }  the same company,
#   "BF Partners S.r.l."  -> "bf partners s r l"  }  two different keys
#
# That cost real matches: "BF Partners S.r.l." sits in Google's partner
# directory and could not be found from "BF PARTNERS SRL". Measured across
# both clouds, 149 of 5,923 vendor names (2.52%) are affected.
_DOTTED_ABBREVIATION = re.compile(r"\b(?:[a-z]\.){2,}", re.IGNORECASE)


def collapse_dotted_abbreviations(text: str) -> str:
    """S.r.l. -> Srl, B.V. -> BV, L.C.M.S. -> LCMS. Leaves prose untouched."""
    return _DOTTED_ABBREVIATION.sub(lambda m: m.group(0).replace(".", ""), text)

DESCRIPTOR_TOKENS = {
    "technology", "technologies", "software", "systems", "solutions",
    "platform", "platforms", "cloud", "labs", "lab", "group", "global",
    "international",
}


def canonical_company_name(value: str | None) -> str:
    """Fold a company name to its comparable core.

    Strips accents, punctuation, and trailing legal suffixes. Used for
    vendor_key and for exact-name identity matching.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii")
    text = text.casefold().replace("&", " and ")
    # Must happen BEFORE tokenising — see collapse_dotted_abbreviations.
    text = collapse_dotted_abbreviations(text)
    tokens = re.findall(r"[a-z0-9]+", text)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def review_company_name(value: str | None) -> str:
    """A looser key used ONLY to flag possible aliases for human review.

    Never used to auto-merge. "Acme Software" and "Acme Systems" collapse to
    the same review key, which is exactly why this can only raise a review.
    """
    return " ".join(
        token for token in canonical_company_name(value).split()
        if token not in DESCRIPTOR_TOKENS
    )


def normalize_domain(value: str | None) -> str:
    """Reduce a URL or bare host to a comparable registrable host."""
    if not value:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def vendor_key(vendor_name: str | None) -> str:
    """Deterministic join key: first 20 hex of sha256 of the canonical name.

    Matches GCP's derivation for any name that canonicalises to something.

    THE FALLBACK MATTERS. `canonical_company_name` strips everything outside
    ASCII, so a name written entirely in another script — 日立製作所,
    株式会社オルターブース, 나무기술 주식회사, شركة مساحة جيمت لتقنية المعلومات —
    canonicalises to "". Returning "" here meant those vendors had no key, and
    any caller that groups by key dropped them on the floor. That silently lost
    25 transactable vendors from Azure's first run.

    So when canonicalisation yields nothing, key off a casefolded,
    whitespace-collapsed form of the original name instead. Still fully
    deterministic; it just cannot merge legal-name variants for those vendors,
    which is the correct amount of caution given we cannot parse the name.

    Only ever returns "" for a genuinely empty name.
    """
    canonical = canonical_company_name(vendor_name)
    if canonical:
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
    raw = " ".join(str(vendor_name or "").split()).casefold()
    if not raw:
        return ""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
