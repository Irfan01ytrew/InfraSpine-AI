# Step X — Natural-language reporting: plan

> Owner asks in plain words ("last month's invoices, unpaid ones") and gets a
> correct report/table — never a guessed number.
> Status: PREPARATION + slice 1 done (parser only). Report execution, API and UI
> are later slices. Branch: `agent/nl-reporting`, base `step-e-gaps-wip` `ca956c2`.

## 1. What exists (mapped 2026-11-28, not rewritten)

| Piece | Where | What it does |
|---|---|---|
| Invoice data | `app/db/finance_models.py` (`FinInvoice`) | `invoice_number`, `vendor_name`/`vendor_id`, `department`, `amount`, `currency`, `invoice_date`, `group_id`, `batch_id`, `status` in `pending / verified / exception / approved / paid / rejected` |
| Finance service | `app/finance/service.py` (`FinanceService`) | `invoices(batch_id, status)`, `batches()`, `summary()`, `exceptions()`, `spend(months, group_id)` — monthly spend by department/currency over `SPEND_STATUSES = (verified, approved, paid)`, already permission-filtered via `readable_groups()` (`finance.read`) |
| Finance API | `app/api/routes/finance.py` | `GET /spend`, batches, invoices, exceptions, approvals, payments — all through `app/platform` |
| Admin reports | `app/api/routes/admin_reports.py` | `usage_report`, `export_usage_report`, `operational_health`, `department_quotas` — admin-only, no NL |
| Knowledge chat | `app/rag/*`, `app/api/routes/chat.py` | Answers from documents, "insufficient evidence" when documents lack the answer; has Roman Urdu query handling (`app/rag/query_analysis.py`, `app/rag/answer.py:460`) but no NL→SQL/report path |

## 2. What is missing

1. **No NL understanding layer.** Nothing turns "last month's invoices, unpaid
   ones" into a structured query. `grep` for `nl_report` / `nl_query` /
   "natural language" in `app/` returns nothing.
2. **No verified report execution.** There is no path that takes a parsed query,
   runs it against the database, and returns numbers computed from real rows with
   the source rows shown. A model must never produce the numbers as text.
3. **No NL report endpoint or UI.** No API route and no frontend page for
   "ask in plain words → table".

## 3. Design (non-negotiable rules from HANDOVER-PROMPT.md §5)

- **Numbers come from the database, never from model text.** The parser only
  produces a `ReportQuery` object. Execution runs SQL via `FinanceService`
  (reuse `readable_groups()` / `require()` — the caller only ever sees groups
  they may read, `finance.read`; non-members get 404, members without the
  permission get 403). The report shows the computed aggregates AND the source
  rows (invoice number, vendor, date, amount, status) so any number can be
  checked by eye.
- **The parser is pure and side-effect free.** `app/finance/nl_query.py` uses
  only the stdlib, imports nothing from the app, touches no DB, no network, no
  secrets. Unknown words are ignored, never interpreted as filters.
- **Untrusted input.** The question text is untrusted: it can only select from a
  fixed allow-list of entities, statuses, date ranges and aggregates. It can
  never change a payee, grant a permission, or widen scope. Vendor names are
  matched as plain substrings (parameterised query, never string-built SQL).
- **No guessing.** If the question has no recognisable report intent, the
  parser returns `intent='unknown'` and the API must answer "I don't understand
  that as a report — try …" with examples, not a made-up table. If a date or
  status phrase is ambiguous (e.g. Roman Urdu "kal" = yesterday *or* tomorrow),
  the parser records a `warning` and picks the documented default (yesterday,
  for a report about the past) — the warning is shown to the user.
- **Timezone.** Dates are computed from the organisation's "today"
  (`app.platform.timezone.org_today`, Asia/Karachi by default). The parser takes
  `today` as an argument so tests are deterministic.

## 4. `ReportQuery` (slice 1 — done)

```python
@dataclass(frozen=True)
class ReportQuery:
    intent: str            # 'invoices' | 'unknown'
    date_from: date | None
    date_to: date | None   # inclusive
    statuses: tuple[str, ...]   # empty = all statuses
    vendor: str | None     # substring match, lower-cased
    department: str | None
    aggregate: str         # 'rows' | 'count' | 'sum' | 'sum_by_vendor' |
                           # 'sum_by_department' | 'sum_by_month'
    currency: str | None   # 'PKR' | 'USD' | ... (3-letter code) or None
    warnings: tuple[str, ...]
    raw: str               # the original question, for audit
```

Date phrases (English + Roman Urdu): `last month` / `pichla mahina` /
`pichle mahine` → previous calendar month; `this month` / `is mahina` →
current month; `this week` / `is hafte` → current ISO week (Mon–Sun);
`last week` / `pichla hafta` → previous ISO week; `today` / `aaj`; `yesterday` /
`kal` (warning: ambiguous); `last 30 days` / `pichle 30 din` → rolling 30 days;
`this year` / `is saal`; `last year` / `pichla saal`; English month names with an
optional year (`november 2026`, `nov 2026`).

Status phrases: `unpaid` / `na-ada` / `baqi` / `jo pay nahi hue` → `pending,
verified, exception, approved` (NOT `paid`, NOT `rejected` — a rejected invoice
is not "unpaid", it is closed); `paid` / `ada` → `paid`; `approved` →
`approved`; `pending` → `pending`; `rejected` → `rejected`; `exception` /
`masla` → `exception`; `all` / `sab` → no filter.

## 5. Slice order (later slices)

1. **Slice 1 — parser (done).** `app/finance/nl_query.py` +
   `tests/test_nl_reporting_parser.py`. Pure stdlib, no DB.
2. **Slice 2 — execution.** `FinanceService.report(query)` — SQL over
   `FinInvoice` restricted to `readable_groups()`, aggregates computed with
   `func.sum`/`func.count`, source rows returned (limit 200, like `batches()`).
   Tests on a throwaway `*_test` DB.
3. **Slice 3 — API.** `POST /api/finance/nl-report` through `app/platform`,
   `finance.read` required, `intent='unknown'` → 422 with examples, warnings
   echoed in the response.
4. **Slice 4 — UI.** Finance page "Ask in plain words" box → table + CSV export
   (reuse `admin_reports` export patterns). Frontend tests + build + typecheck.
5. **Slice 5 — docs + gates.** `CHANGES-step-X.md`, full suite ≥1347 passed,
   Alembic check (no migration expected), merge to main only if green.

## 6. Test list (slice 1)

`tests/test_nl_reporting_parser.py` — 25 tests, all pure (no DB, no network):
dates (last/this month, weeks, today/yesterday, rolling 30 days, years, named
months, leap-year February edge), statuses (unpaid mapping incl. rejected
exclusion, paid, approved, pending, exception, all), aggregates (count, sum,
by vendor/department/month), vendor/department extraction, currency, unknown
intent, warnings for "kal", mixed Roman Urdu + English questions, and the
owner's example sentence end to end.
