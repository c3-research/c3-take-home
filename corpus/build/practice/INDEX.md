# C3 corpus

Reference material for the C3 services in this assessment. Read-only.

| Layer | What it holds |
| --- | --- |
| `background/` | Open-access research on distributed computing, concurrency and real-time systems (LIPIcs proceedings: DISC, CONCUR, OPODIS, SAND, ECRTS). General theory, not C3-specific. Attribution in `background/SOURCES.md`; paper list in `background/paper-catalog.md`. |
| `platform/` | Shared C3 platform contracts that every service builds on. |
| `service/<X>/` | Per-service design notes, runbooks, API reference and changelog. |

## Platform documents

| File | Status |
| --- | --- |
| `platform/changelog.md` | - |
| `platform/gpu-api-v1.md` | SUPERSEDED |
| `platform/gpu-api-v2.md` | SUPERSEDED |
| `platform/gpu-api-v3.md` | CURRENT |
| `platform/idempotency.md` | CURRENT |
| `platform/leases-and-fencing.md` | CURRENT |
| `platform/runtime-v3.md` | CURRENT |

Documents marked SUPERSEDED are kept for reference only. Where they disagree with a CURRENT document, the CURRENT document is right.

## Services

- `service/A/` (12 files)
- `service/B/` (12 files)

## Searching

Everything is plain text or markdown; `grep -rn` works across all layers. Log tokens and error codes seen in service logs are the best search keys.
