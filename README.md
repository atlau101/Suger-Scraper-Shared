# shared/

Code that must mean the same thing on every cloud.

| Module | Contains | Why it's here |
|---|---|---|
| `normalize.py` | `canonical_company_name`, `review_company_name`, `normalize_domain`, `vendor_key`, `LEGAL_SUFFIXES`, `DESCRIPTOR_TOKENS` | `vendor_key` is the cross-cloud join key. If two clouds canonicalise differently, one company becomes two and the multi-cloud signal — the strongest ICP evidence we have — silently breaks. |
| `identity.py` | `SearchCandidate`, `select_candidate`, `dedupe_candidates`, `geo_status`, lookup-state vocabulary | Every partner directory is a fuzzy search engine. Forking this is how "confirmed" stops meaning the same thing on each cloud. |
| `vendors_csv.py` | `VENDOR_COLUMNS`, `write_vendors_csv`, `read_vendors_csv` | The seam. Any cloud that emits a valid `vendors.csv` inherits Stages 3–5 unchanged. |

Promoted from GCP when Azure landed (2026-09-07), per `../ARCHITECTURE.md`'s
"promote on second use" rule.

## Rules

- **Nothing cloud-specific goes in here.** No Google, Azure, or AWS concepts.
  Everything is pure — no I/O.
- **`LEGAL_SUFFIXES` is pinned to GCP's set** and covered by a test in
  `../Azure/azure_marketplace/tests/`. Changing it re-keys vendors; that is a
  coordinated change across both clouds plus a re-key of existing artifacts,
  never a drive-by edit.

## Resolved 2026-09-07 — the suffix fork is closed

Both GCP modules now `from normalize import LEGAL_SUFFIXES`. Neither defines a
local copy, and a test in `../Azure/azure_marketplace/tests/` fails if anyone
re-adds one.

**What was wrong.** Each GCP file kept its own suffix list and they had drifted
— they agreed on only 22 of 34 entries. `gcp_marketplace.py` alone stripped
`{ad, asa, kgaa, se, srl}`; `gcp_partner_status.py` alone stripped
`{aps, as, kft, kk, llp, lp, pty}`. So the same company canonicalised
differently depending on which stage looked at it: "Trafficguard Pty Ltd" was
`trafficguard pty` in Stage 1 and `trafficguard` in Stage 2. 23 of GCP's 1,699
vendors (1.35%) were affected.

**What it cost.** Missed partner matches. Azure inherited Stage 1's blind spot
and recorded "Lucid Labs Pty Ltd" and "BUI (Pty) Ltd" as
`not_found_in_searched_source` when the directory lists them as "Lucid Labs"
and "BUI" — both confirm correctly under the reconciled list.

**The fix** is the union of both lists (34 entries). `vendor_key` changes for
the affected ~1-2%, so both clouds need a re-run of qualify/export for their
keys to agree; Azure's was re-run on 2026-09-07. Already-published GCP
workbooks carry the old keys.

## Also fixed 2026-09-07 — empty vendor_key on non-Latin names

`canonical_company_name` strips everything outside ASCII, so a name written
entirely in another script (日立製作所, 株式会社オルターブース, 나무기술 주식회사)
canonicalised to `""` and got an empty `vendor_key`. Any caller grouping by key
dropped those vendors silently — it cost Azure 25 transactable vendors on its
first run.

`vendor_key` now falls back to hashing a casefolded, whitespace-collapsed form
of the original name. Still deterministic; it simply cannot merge legal-name
variants for those vendors, which is the right amount of caution when the name
cannot be parsed. Only a genuinely empty name returns `""`.

## Still deliberately out

`sl`, `sro`, `doo`, `dooel`, `ou` are absent from `LEGAL_SUFFIXES`. Adding them
would be more correct (~196 of 15,311 Azure vendors, mostly Italian and
Spanish) but they were not part of the GCP divergence, so they were left out to
keep the re-key blast radius to exactly what was broken.

## Importing

Both Azure stages do:

```python
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
```

`shared/` currently sits outside both cloud git repos and is tracked by
nothing. See the "Known structural issue" section of `../Azure/AGENTS.md`.
