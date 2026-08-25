# Sourcing agent POC

Compares supplier quotations for semiconductor grade wet chemicals and produces
two outputs: a quote comparison dashboard and an approval package summary.

Three quotes arrive as PDFs in three different layouts, three currencies and
three units of measure. The system normalizes them to a common basis, applies
the category strategy's rules in order, and shows the trail that produced the
recommendation.

## The one design rule

**The model never does arithmetic.**

| Deterministic Python | Gemini on Vertex AI |
|---|---|
| Currency, density and gallon conversion | Turning extracted strings into typed values |
| Freight uplift by Incoterm | Resolving a CAS number a quote omits |
| Landed cost, discounts, basket totals | Mapping quote text to the compliance checklist |
| Gates 1, 2 and 3, ranking, promotion rule | Explaining a finished run, drafting the memo |

The engine under `backend/app/engine/` imports nothing from Google Cloud, the
database or any model API. It takes dataclasses in and returns dataclasses out,
which is why the same inputs always produce the same recommendation and why the
whole rule set can be tested in under a second.

## Pipeline

```
upload PDFs ──► Cloud Storage ──► Document AI ──► Gemini ──► automated checks
                                                                    │
                                                                    ▼
                                            engine.evaluate() ──► evaluation_run
                                                                    │
                                              ┌─────────────────────┴──────────┐
                                              ▼                                ▼
                                         dashboard                     approval package
```

Extraction, normalization and validation run as one background job per upload;
the browser polls document status. Evaluation is synchronous and takes
milliseconds.

## Deploying

```bash
PROJECT_ID=my-project DOCAI_PROCESSOR_ID=abc123 DB_PASSWORD=secret ./infra/deploy.sh
```

Creates the bucket, Cloud SQL instance, Artifact Registry repository and service
account, then builds and deploys both containers. Tables are created on
start-up, so there is no migration step.

Setting it up by hand instead: [infra/ENV.md](infra/ENV.md) lists every
environment variable, the service account roles, the Cloud Run settings that are
not defaults, and what each failure mode looks like.

Two flags in the script are load bearing rather than tuning:

`--no-cpu-throttling` on the backend. Extraction runs in a FastAPI background
task after the response has been sent, and Cloud Run's default throttles CPU to
near zero at exactly that point. Without this every upload sticks at
`PROCESSING`.

`--set-env-vars "^@^..."` makes `@` the separator, so a database password
containing a comma does not split the variable list.


## What happens on first run

Startup creates the 18 tables if they are absent and loads reference data:
densities, the FX rate, ceiling prices, required volumes, the compliance
checklist and the rule thresholds. That is configuration the engine cannot run
without, and it is idempotent - existing rows are left alone.

No supplier, quote or comparison is ever seeded. Those exist only once a real
quotation has been uploaded and extracted.

One-time setup, under *Policy in force*: upload the category strategy document
and the SAP BW extract. Both are optional — the seeded defaults work without
them, though the concentration check falls back to an assumed split until the
PO history is loaded.

Then, per comparison: create it, upload the quote PDFs, wait for each to reach
`READY`, and press Evaluate.

## Layout

```
backend/app/
  engine/       pure calculation - no cloud, no database, no model
    calc.py       normalization, freight, basket totals, line total check
    gates.py      Gate 1 compliance, Gate 2 MOQ, Gate 3 ceiling materiality
    ranking.py    base rank, promotion rule, award allocation
    runner.py     orchestrates the above into one run result
  ingest/       Document AI, Gemini, category strategy, historical CSV, checks
  agent/        ADK agent and its read-only tools over a stored run
  render/       deterministic approval memo
  api/          HTTP routes
  models.py     schema - created on startup, no separate .sql file
  seed.py       reference data: densities, FX, ceilings, thresholds
frontend/       vanilla JS SPA served by nginx
infra/          Cloud Run deployment
```

There is no test suite and no sample data in this build.

## Data model

Four tables change: `comparison`, `source_document`, `quote` / `quote_line`, and
`evaluation_run`. The rest are reference: `material`, `supplier`, `benchmark`,
`demand`, `freight_policy`, `compliance_requirement`, `policy_config` and
`historical_price`.

Two things are load bearing:

**Provenance travels with every extracted field** — page number, confidence and
the raw text, stored as JSON on the row. A number on the dashboard can be traced
back to where it was read from, which is what section 7 of the requirements
asks for.

**A run is an immutable snapshot.** It stores the quotes it evaluated, the
thresholds and FX rate in force at the time, and the complete result. Re-running
never rewrites an earlier run, so an approved memo keeps saying what it said
when it was approved. Changing a threshold changes future runs, never past ones.

## Document AI processor schema

`backend/app/ingest/docai.py` is the only file that knows the processor's entity
names. Everything downstream works in this application's own names.

| Processor entity | Becomes | Used for |
|---|---|---|
| `supplier_name` | `supplier_name` | Supplier identity, upserted on first sight |
| `quote_number` | `quote_no` | Traceability |
| `quote_date`, `validity_date` | `quote_date`, `valid_until` | Traceability |
| `currency` | `currency` | FX conversion (3.1) |
| `incoterm` | `incoterm` | Selects the fixed freight uplift (3.2) |
| `payment_terms` | `payment_terms_text` | Promotion rule condition 3 |
| `lead_time` | `lead_time_text` | Promotion rule condition 4 |
| `total_amount` | `total_amount_stated` | Cross-checked against the line sum |
| `compliance_text_block` | `compliance_text` | Checklist mapping (5.1, 5.5) |
| `discount_text_block` | `discount_text` | Basket discount (3.3) |
| `moq_text_block` | `moq_text` | Gate 2 (5.2) |
| `line_item.cas_number` | `cas_no` | The cross-supplier match key |
| `line_item.item_description` | `supplier_description` | Display only, never matched on |
| `line_item.quantity` | `quantity` | Line total check (7) |
| `line_item.unit_of_measure` | `uom` | Litre conversion (3.1) |
| `line_item.unit_price` | `unit_price` | Everything downstream |
| `line_item.line_total` | `line_total_stated` | Line total check (7) |

Three of these are free-text blocks covering the whole quote rather than single
values. The normalizer turns them into typed fields: the MOQ block is split per
material so Gate 2 has a number for each line, the discount block becomes a
percentage plus a condition type, and the compliance block is matched against
the checklist with the supporting sentence quoted back.

An entity type the map does not recognise is logged and returned in
`unmapped_types` rather than dropped silently, so a processor schema change is
visible. Field names are matched with spaces and hyphens folded to underscores,
so `unit price` and `unit_price` both resolve.

`tests/test_docai_mapping.py` asserts the schema field by field, and runs one
document through the real pipeline — mapping, normalization, persistence and
validation — with only the Google call stubbed.

## Freight adjustment

The percentages are fixed policy, not read from the quote: **DAP 0%, FOB 5%,
EXW 9.5%**. Document AI extracts the Incoterm code, the named place is dropped,
and the code selects the row.

An Incoterm with no policy row is the case that matters. A silent 0% would
understate that supplier's landed cost and could flip the ranking, so the run
records `freight_policy_matched: false`, raises a warning, and the dashboard
shows "no policy for CIF" in red instead of a percentage. Adding the Incoterm to
`freight_policy` is then a one-row change.

## Reference data

Both sources the spec names are uploaded under *Policy in force*, and both are
one-time setup. Seeded defaults stand until a document replaces them, so the
system works from a cold start.

### Category strategy document (spec 2.3)

A PDF or Word document, read by Gemini — there is no Document AI processor for
it. Four things come out:

| Extracted | Lands in | Drives |
|---|---|---|
| Target and ceiling price per material | `benchmark` | Outlier flagging (4.1), Gate 3 (5.3) |
| Compliance checklist with tiers | `compliance_requirement` | Gate 1 (5.1), promotion rule (5.5) |
| Dual-sourcing / concentration threshold | `policy_config` | Category strategy alignment (5.5) |
| Approved supplier list | `approved_supplier` | Supplier validation (2.3) |

Only what the document states is changed. A material it does not mention keeps
its existing ceiling; a threshold it omits keeps its default. Nothing is blanked
by omission, and a guess would silently change policy — so anything the model
cannot place is reported under "Not applied" rather than applied.

Checklist codes are constrained to the seven the engine knows, because Gate 1
and the promotion rule are keyed on them. A requirement outside that set is
surfaced as text rather than given an invented code that no rule would match.

While `approved_supplier` is empty every quoting supplier counts as approved, so
the check does not fire before there is a list to check against.

### Historical purchase prices (spec 2.2)

An Excel workbook with two sheets, or a single-sheet CSV of either shape.
Headers are matched loosely; rows are matched to quote lines on CAS number, and
a row whose CAS falls outside the category is reported rather than absorbed.

**Price Summary** is the benchmark section 4.2 asks for — average, min, max and
last invoiced price per material. It drives the variance shown beside the
ceiling on every line.

**PO Price History** is one row per purchase order line. It adds what the
summary cannot: which vendors the spend actually went to. That turns the
dual-sourcing concentration check from an assumed split into a measured one —
the dashboard shows what each supplier holds today against what the award
proposes, and a vendor already above the threshold is reported.

The PO lines also supply the period and line count behind each benchmark, and
fill in any material the summary sheet omits — computed from the lines and
labelled as derived rather than presented as a supplied figure.

## Decisions the requirements document leaves open

Each is configurable in `policy_config` and visible under *Policy in force*.

| Question | Setting | Chosen |
|---|---|---|
| Line total mismatch tolerance | `line_total_tolerance_pct` | 0.5% |
| Award split for the concentration check | `primary_allocation_pct` | 60/40, labelled as an assumption in the memo |
| Confidence below which an extracted field is flagged | `extraction_confidence_threshold` | 0.8 |

Three more, resolved in code: a supplier fails Gate 2 if **any** line breaches
the MOQ allowance; the promotion rule tests each supplier once against the one
immediately above it with no cascading re-ranks; and Gate 2 converts MOQ to
litres rather than converting demand into each supplier's unit as the spec's
table does — the ratio, and so the verdict, is identical either way.

Two questions turned out not to be open at all. **Discount ordering** is moot:
freight and discount are both percentages of the whole basket, so they commute,
and section 3.3 puts the discount on the landed sum regardless. **Rounding** is
settled by the spec's own numbers — its section 4.1 grid at two decimals extends
to 176,420 while section 3.3 states 176,400, so per-litre rounding is
presentation only and totals come from full precision.

## Not in scope

No authentication, no Secret Manager, no test suite, and no human review step
before evaluation.
The automated checks flag low extraction confidence and line total mismatches as
badges on the dashboard rather than blocking, so a misread field is visible at
the point of decision but does not stop the run. Given that, treat this as a
demonstration environment and keep real supplier data out of it.
