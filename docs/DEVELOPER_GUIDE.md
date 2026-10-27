# Advance Motor Insurance Solution — Developer Guide

Audience: developers who will run, extend, test and deploy the system. Business rules come from the FRD v1.0 and its Addendum; every tariff, rate, limit, FX rate and treaty in the seed data is an **illustrative placeholder** that must be replaced with approved figures before go-live.

---------------------------------------------------------------------

## 1. Quick start

    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py seed_demo            # reference data, accounts, flex examples, demo logins
    python manage.py load_demo_data       # optional: 1,000 policies, 300 claims (~90 s)
    python manage.py runserver

Logins (password `demo1234`): `admin`, `pm1`, `uw1`, `agent1`, `broker1`, `claims1`, `claimsmgr1`, `finance1`, `customer1`.
Tests: `python manage.py test` (about 55 tests, ~35 s). Requirements: Python 3.11+ (developed on 3.13), Django 5.2, openpyxl, reportlab, Pillow (logo in PDFs).

### Settings and environment variables (`config/settings.py`)

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev value | **Set in production** |
| `DJANGO_DEBUG` | `1` | Set `0` in production |
| `DJANGO_ALLOWED_HOSTS` | `*` | Comma separated |
| `POSTGRES_DB/USER/PASSWORD/HOST/PORT` | unset | If `POSTGRES_DB` is set PostgreSQL is used, otherwise SQLite (WAL mode) |
| `EMAIL_BACKEND`, `DEFAULT_FROM_EMAIL` | console backend | Server e-mail (policy documents, reminders) |
| `MOTOR_VAT_RATE` | `0.05` | Oman VAT |
| `MOTOR_REPAIR_DELAY_DAILY` | `0.000` | Daily compensation for repair delay beyond the 30-day SLA |
| `PAYMENT_GATEWAY` | `demo` | Key in `payments/gateways.py` |
| `OLLAMA_HOST`, `OLLAMA_TIMEOUT` | `http://127.0.0.1:11434`, 180 | Local Ollama (Company setup can override the host) |
| `IFRS17_EXPENSE_IACF` | `1` | Expense commission as incurred (IFRS 17.59(a)) instead of deferring |
| `REDIS_URL` | local | Only if you schedule jobs with Celery |

Other constants: `BASE_CURRENCY="OMR"`, `RENEWAL_REMINDER_DAYS=(30,7,0)`, `REPAIR_SLA_DAYS=30`, `REPAIR_INTERIM_PCT=70`, `SURVEYOR_SLA_DAYS=3`, `PAYMENT_LINK_VALIDITY_DAYS=7`, `SHARE_LINK_VALIDITY_DAYS=14`, `DEFAULT_DEPRECIATION_PCT=15`, `APP_NAME`.

---------------------------------------------------------------------

## 2. Architecture

Classic Django monolith; server-rendered templates, plain Django forms with custom widgets, a little vanilla JS, no front-end build step.

| App | Responsibility |
|---|---|
| `core` | Currency, document sequences, audit log, user profile/roles, **Company** (letterhead), **FlexField**, **DemoRun**, clock, flex-field engine, PDF kit, demo loader, widgets, template tags (`money`, `icons`) |
| `motor` | Products/covers/rates, customers, vehicles, quotes/proposals, policies, endorsements, drivers, vouchers, commissions, premium register, sharing (WhatsApp/Outlook), policy PDF |
| `claims` | Claims, providers, estimates, payments, recoveries, subrogation, letters, **damage map + AI estimate** (`claims/ai.py`) |
| `reinsurance` | Treaties (quota share, surplus, XOL with MDP), cessions, facultative hold |
| `payments` | Receipts and online payment requests, demo gateway, public checkout |
| `accounting` | Chart of accounts, journals (IFRS 17 PAA), debit/credit notes and VAT invoices, trial balance, IFRS 17 reports |

Pattern: **views are thin, business rules live in `services.py`** of each app (`motor/services.py`, `claims/services.py`, `accounting/services.py`, `payments/services.py`, `reinsurance/engine.py`). Forms validate input; services mutate state inside transactions. Add behaviour in services and call it from views, commands and the demo loader alike.

### Roles and permissions (`core/permissions.py`)
Roles are Django groups created by migrations/seed: ADMIN, AGENT, BROKER, PRODUCTION_MANAGER, UNDERWRITER, CLAIMS_HANDLER, CLAIMS_MANAGER, FINANCE, CUSTOMER. `@role_required(...)` (Admin always allowed) guards views; `scope(user, queryset)` restricts data (producers see their own business, customers their own policies, staff everything). The navigation menu flags are built in `core/context_processors.py`; when you add a screen, add both the decorator and the menu flag. Company setup, Demo data: Admin only. Flex fields: Admin, Production Manager, Underwriter.

### Audit
`core/audit.py` records create/update/delete of audited models (signals). `with audit.suspended():` skips it (used by bulk loaders). Journal lines/entries are excluded because they are already their own audit trail.

### The clock (`core/clock.py`)
Business code must call `clock.today()` instead of `date.today()` for anything that stamps a document (numbers, journal dates, claim paid dates, endorsement dates). `with clock.posting_date(d):` makes the whole block act "as of" `d`; the demo loader uses it to back-date three years of history. Pure UI defaults (form initial dates) still use `date.today()`.

---------------------------------------------------------------------

## 3. Domain model (key points)

### Products, rating, discounts
`Product` (COMP, TPL) → `ProductCover` → `Cover` (OD, TP, riders) → `RateTable` (OD rate % by vehicle type/usage/age; TP flat premium by usage/cc). `Product.depreciation_pct` (default 15 % p.a.) drives depreciation. TPL is `is_tariff_regulated`: **no discounts or vouchers ever apply to it**.
`Discount` rows: `NCD` and `FLEET` thresholds (the old "voluntary excess" discount was removed). Additional manual discount and voucher discount apply only to the *discountable base* (OD + riders).

`services.rate_risk()` prices one vehicle; `price_quote()` totals a quote (fees, VAT) and returns `voucher_error`/`voucher_discount`; `reprice_proposal()` re-prices a stored proposal and drops a voucher that has become invalid. Vehicle age for rating is `start.year − model year`.

### Customers, vehicles and uniqueness
`Vehicle.chassis_no` is unique, and `Vehicle.vehicle_key` = `MAKE|MODEL|YEAR|CHASSIS` is unique (set in `save()`). `RiskForm.clean()` rejects a chassis that already exists with a different make/model/year with a clear message; the database constraint is the last line of defence. A vehicle can have only one **active** policy at a time (checked in `convert_to_proposal`).

### Quote → policy
`Proposal` statuses QUOTE → PROPOSAL → PENDING (needs approval) → APPROVED → ISSUED. `approval_reasons()`/`can_decide()` implement authority limits by role (`ApprovalLimit`): sum insured, manual discount, fleet size. **Voucher discount is excluded from authority checks** (`authority_disc_pct`). `issue_policy()` copies risks, drivers, flex values and voucher discount; sets `Vehicle.base_value/base_date` (start of the depreciation clock); redeems the voucher; allocates treaties (`reinsurance.engine`); computes commissions; posts accounting (`acc.on_policy_issued`) and creates the DN and VAT invoice.

### Renewal and depreciated value
`renew_policy_as_quote(policy)`: new start = max(old end + 1, today); for comprehensive cover the sum insured per vehicle = `min(depreciated value, previous sum insured)`. `depreciated_value(base, base_date, as_of, pct)` = `base × (1 − pct)^(days/365.25)` (reducing balance). `ProposalRisk.si_note` records the explanation shown to the user. Policies that are ACTIVE or EXPIRED can be renewed; an open renewal quote is reused instead of duplicated. In the UI the customer screen and the quote screen list all policies of the selected customer (`/api/customer-policies/`) with Renew / Open-renewal buttons.

### Endorsements (`Endorsement.TYPES`)
Extension, cancellation, transfer of ownership, engine change, colour change, usage change, additions, **REGNO** (change registration number: updates vehicle and policy risk, clash check on the new plate) and **ADDDRIVER** (adds a `Driver`; young-driver loading rules apply). Flow: `quote_endorsement()` (pro-rata premium effect, fee, VAT) → `apply_endorsement()` (mutates, creates DN/INV or CN, adjusts cessions/commission, posts accounting).

### Drivers
`Driver` belongs to a `ProposalRisk` or a `PolicyRisk` (copied at issuance). Entered in the vehicle card (JS writes the hidden `drivers_json`; `RiskForm.clean_drivers_json` validates each row with `DriverForm`). At claim registration the handler may select the driver; age and the young-driver flag then flow into the excess calculation.

### Vouchers
`Voucher(code, discount_type PERCENT|FIXED, value, currency, valid_from/to, max_uses, min_net_premium, products, customer, active)`; `status`, `label`, `used_count` are derived. `validate_voucher()` checks existence, dates, usage cap, product/customer restriction and non-regulated product. A fixed voucher is converted into a percentage of the discountable base at pricing time. `VoucherRedemption` is created at issuance (one per policy). Screens: `/vouchers/` (list, bulk issue with auto-generated codes, edit, activate/deactivate).

### Flex fields (user-defined fields)
`FlexField(entity CUSTOMER|VEHICLE|POLICY|CLAIM, key, label, field_type, choices, required, order, active)`. Values live in a `flex` JSONField on Customer, Vehicle, Proposal, Policy and Claim. `core/flex.py`: `attach(form, entity, initial, prefix)` adds the dynamic fields to a form (prefixes `cflex_`, `pflex_`, `vflex_`; bound fields available as `form.flex_customer`, `form.flex_policy`, `form.flex_vehicle`, `form.flex_claim`), `extract(form, entity)` returns the JSON dict, `display(entity, data)` returns label/value rows for detail screens and PDFs. Include `templates/includes/flex.html with fl=form.flex_xxx` in a template. Deactivated fields keep their data. To support a new entity: add a JSONField, call `attach`/`extract` in its form/view, add it to `FlexField.ENTITIES`.

### Company setup
`core.Company` singleton (`Company.get()`): names, CR, VAT and FSA numbers, address, logo, bank details, invoice footer, default phone code, `public_base_url`, Ollama host/model. Used by every PDF (`core/pdfkit.letterhead`), the public pages and sharing links.

---------------------------------------------------------------------

## 4. Documents (PDF)

`core/pdfkit.py` wraps reportlab (letterhead, grid, key-value, totals). Documents: policy schedule (`motor/pdf.py`), debit note / credit note / VAT tax invoice and receipt voucher (`accounting/documents.py`). Add a document: write a function returning bytes, register it in `motor/sharing.available_documents()` / `render_document()` so it also appears in the "Send documents" screen.

Arabic text: reportlab needs shaping; the project ships Latin text with the OMR symbol handled by `core/templatetags/money.py` (see §10). Add a shaped Arabic font if you need bilingual documents.

### Premium register (`motor/reports_ext.py`)
`register_rows()` produces one line per issued policy and per priced endorsement/cancellation in the period, with base-currency columns at the booking rate, voucher discount, base commission, ceded premium, paid and balance. Screen, CSV and XLSX share the same rows (`?format=csv|xlsx`). Respects `scope()`.

---------------------------------------------------------------------

## 5. Payments (`payments` app)

`Payment(method CASH|CHEQUE|TRANSFER|POS|ONLINE, status PENDING|PAID|FAILED|CANCELLED|EXPIRED, receipt_no RV/yyyy/n, token)`.
- `record_receipt()` – counter/cheque/transfer/POS money: creates a PAID receipt voucher immediately.
- `request_online_payment()` – creates a PENDING request with a secret token; the public page `/pay/<token>/` takes the card details and calls `pay_online()`.
- `mark_paid()` – sets PAID, allocates the receipt number, posts the accounting entry (Dr cash, Cr premiums receivable).
- Over-payment beyond the balance due is rejected (`PaymentError`).
- Policy `paid_amount`, `balance_due`, `payment_status` (UNPAID/PARTIAL/PAID) are derived from PAID payments.

### Gateways (`payments/gateways.py`)
`BaseGateway.charge(payment, number, expiry, cvv, holder) -> GatewayResult`. `DemoGateway` (`is_test = True`) validates Luhn/expiry and simulates outcomes: `4242 4242 4242 4242` success, `4000 0000 0000 0002` declined, `…9995` insufficient funds, `…0069` expired. **To add a real gateway** (Thawani, OmanNet, Stripe, …): subclass `BaseGateway`, implement `charge()` (or a redirect/webhook flow: create the session in `request_online_payment`, confirm in a webhook view that calls `mark_paid`), register it in `get_gateway()`, set `PAYMENT_GATEWAY`. Never store card numbers: the demo gateway keeps only the last four digits. The checkout page shows the test-card help only when `gw.is_test`.

Public links need `Company.public_base_url` (otherwise the request host is used).

---------------------------------------------------------------------

## 6. Sending documents (`motor/sharing.py`, `motor/views_ext.py`)

- **Outlook desktop**: the server builds an RFC-822 `.eml` with header `X-Unsent: 1` and the PDFs attached; opening it in *classic* Outlook shows an editable draft. (The "new" Outlook for Windows does not open `.eml` drafts; use "E-mail from server" there.)
- **WhatsApp desktop**: link `whatsapp://send?phone=<number>&text=<message>`; WhatsApp cannot be given files by a link, so the message contains an **expiring secure link** (`DocumentShare`, public page `/d/<token>/`, download `/d/<token>/<key>.pdf`, validity `SHARE_LINK_VALIDITY_DAYS`). A WhatsApp Web link is offered as well.
- **Server e-mail**: Django `EmailMessage` with PDF attachments, logged in `PolicyDocument`.
Phone numbers are normalised with the company default code (968). Each share records the documents, channel, expiry and download count.

---------------------------------------------------------------------

## 7. Accounting and IFRS 17 (`accounting` app)

All journals are balanced and in base currency (OMR) at the policy's booking rate. Chart of accounts (`accounting/services.CHART`, loaded by `ensure_accounts()`): 1110 Cash, 1120 Premiums receivable, 1130 DAC, 1210 Reinsurance asset – remaining coverage, 1230 Reinsurance recoverable – incurred claims, 2110 **LRC**, 2120 **LIC**, 2210 VAT output, 2220 Commission payable, 2230 Reinsurance premium payable, 4110 Insurance revenue, 4210 Reinsurance recoveries, 4300 Policy fee income, 5110 Incurred claims, 5120 Acquisition cash flows, 5130 Reinsurance premium allocation. Each account carries an `ifrs17_line` label for the statement mapping.

| Event | Entry type | Posting (summary) |
|---|---|---|
| Policy issued | ISSUE | Dr receivable; Cr LRC (net premium), Cr VAT, Cr fee income; Dr reinsurance asset / Cr reinsurance payable; commission (expense or DAC) |
| Endorsement | ENDORSE | Same, for the premium delta (credit note for refunds) |
| Receipt | RECEIPT | Dr cash, Cr receivable |
| Revenue recognition | EARN | Dr LRC, Cr insurance revenue for the earned fraction (straight line over the period); allocates reinsurance premium to expense |
| Claim reserve/approval | CLAIM | Dr incurred claims, Cr LIC; reinsurance share Dr 1230 / Cr 4210 |
| Claim payment | CLAIM_PAY | Dr LIC, Cr cash |
| Recovery / subrogation / excess refund | RECOVERY … | reduce claims expense / settle receivables |

`recognise_revenue(through)` is **idempotent** (posts only the delta since the last run per policy) and is called by the *Recognise revenue* button on the IFRS 17 screen (also from a scheduled job if desired). `sync_claim()` keeps LIC equal to the claim's current gross incurred amount by posting differences.

Reports (`/accounting/…`): transaction report (filter by date/type/account/policy/claim, CSV export), trial balance with opening/movement/closing, IFRS 17 page with four reconciliations (LRC, LIC, reinsurance ARC, reinsurance AIC – each shows an *unreconciled difference* if any) and the insurance service result, and the DN / CN / VAT-invoice register with PDFs.

**Not modelled** (needs actuarial/finance decisions): discounting of future cash flows, risk adjustment, onerous-contract loss component, reinsurance non-performance risk, financial income/expense, transition. Motor cover ≤ 1 year qualifies for the PAA; if you adopt DAC amortisation set `IFRS17_EXPENSE_IACF=0`. Have your actuarial and finance teams validate the mapping before using it for reporting.

---------------------------------------------------------------------

## 8. Claims, damage map and AI estimate

### Claim flow
`register_claim()` validates cover period and policy status, calculates the excess breakdown, assigns a surveyor, creates the claim and posts the reserve. Then reserve → surveyor → garage estimate (`check_estimate`, flags labour above market) → `approve_claim()` → 70 % interim / final payments (`pay_claim`) → `complete_repair()` (provider rating, SLA delay compensation) → recoveries/subrogation → `close_claim()`. Every financial step calls `accounting.services` so the ledger follows the claim.

### Total loss and depreciated value
If the claim is marked **total loss**, `total_loss_value()` returns `base_value × (1 − pct)^(years)` from the vehicle's `base_value/base_date` (or the sum insured when unknown), **capped at the sum insured**; it is stored in `Claim.depreciated_value` and used as the default approved value. The claim form shows the value live (`/claims/api/risk/<id>/`).

### Damage map
`templates/includes/damage_map.html` is an inline SVG (top view, front at the top) with 15 clickable panels (`ZONES` in `claims/ai.py`). JS (`static/js/extras.js`) cycles a panel through Minor → Moderate → Severe → none and writes the JSON `{zone: severity}` into the hidden `damage_zones` field. It is also shown read-only on the claim page. Add a panel: add a `<path data-zone="…">` and an entry in `ZONES` and in the JS `NAMES` map.

### Estimate pipeline (`claims/ai.py`)
1. `clean_zones()` validates the zones/severities.
2. `baseline()` prices the damage from **company history**: approved, non-total-loss repair claims with damage zones, weighted by similarity (vehicle type, make, overlapping zones/severity), expressed as cost per "damage point"; with fewer than a few comparable claims it blends in a transparent rule-of-thumb table (`rule_of_thumb`). Result: expected/low/high + the most similar past claims.
3. If a model is selected, `ask_ollama()` calls `POST {host}/api/chat` with `format: "json"`, giving the vehicle, damage description, the baseline and the closest cases; the answer (expected, low, high, likely parts, labour hours, total-loss risk, reasoning) is **bounded against the company-data band** (0.6× low … 1.8× high when ≥5 comparables) and flagged when pulled back.
4. If Ollama is unreachable or answers badly, the statistical estimate is returned with a warning; the screen never fails.
5. Amounts are converted from OMR to the policy currency at the policy rate. The estimate is stored on the claim (`ai_estimate`, `damage_zones`) and is decision support, never an approved settlement.

**Ollama**: `list_models()` calls `GET /api/tags` and the *AI models* screen lists every installed model with family/size/quantisation; Admin/Claims Manager choose the default (`Company.ollama_model`); the claim form offers all installed models in a drop-down. Install: `ollama serve`, `ollama pull llama3.2` (any chat model works; larger models follow the JSON instruction better). Everything runs locally; no claim data leaves the network. Change the host under Company setup or `OLLAMA_HOST`.

---------------------------------------------------------------------

## 9. Demo data (`core/demo.py`)

`load_demo(policies=1000, claims=300, seed=42, progress=cb)` builds a deterministic (seeded) book through the **real services**:
- 7 vouchers (active, expired, scheduled, fixed in OMR/USD, capped) + 8 single-use promo codes.
- Customers (individuals incl. expatriates, ~6 % corporate fleets), vehicles from a 38-model catalogue by type/usage, valid plates and 17-char VINs (unique).
- Policy chains over ~3 years (new business then renewals through `renewal_of`, depreciated sums insured), 70 % comprehensive / 30 % TPL, currency mix (OMR ~82 %, USD, AED, SAR, GBP), producers (4 agents, 3 brokers, PM), additional drivers (~35 % of private comprehensive), vouchers (~10 %), ~12 % endorsements (all types incl. REGNO/ADDDRIVER), ~4 % cancellations, lapsed policies marked EXPIRED.
- Receipts: cash, transfer, POS, cheque and online (via the demo gateway path), instalments and unpaid balances.
- 300 claims across collision/theft/fire/natural/TP property/TP injury/other with correlated damage zones, total losses (~5–6 %), approvals, repairs, payments, subrogation and recoveries, rejected and open claims.
- IFRS 17 revenue recognition runs to month-end and today.
- Treaties for the last three years are created by `seed_demo`.

All records carry `is_demo=True` (Customer, Vehicle, Proposal, Policy, Claim, Voucher, Payment, JournalEntry, FinancialDocument, UserProfile). `unload_demo()` deletes in dependency order (claims → journals → documents → receipts → shares → policies → proposals → vehicles → vouchers → customers; optional demo users) and never touches unflagged data. `demo_counts()` feeds the UI. Web page `/demo-data/` (Admin) starts a background thread and polls `/demo-data/status/<id>/` (`DemoRun`); the CLI commands are `load_demo_data` and `unload_demo_data`. Only one job can run at a time. With multiple web workers run the CLI instead of the page.

To extend: add a generator step in `Loader`, set `is_demo` on what it creates, and add it to the unload order.

---------------------------------------------------------------------

## 9a. Budgeting, forecasting and outliers (`budgeting` app)

**Models.** `BudgetVersion` (name, year, kind BUDGET/FORECAST, status DRAFT→APPROVED→LOCKED, parent, method, cutoff, `is_demo`) and `BudgetLine` (version, product, metric, month, amount; unique per cell). Stored metrics: GWP, EARNED, CLAIMS, COMM, POLICIES. Loss, expense and combined ratios are derived, never stored.

**Actuals (`actuals.py`)** come only from the IFRS 17 ledger, in OMR: GWP = net credits to 2110 on ISSUE/ENDORSE entries; EARNED = 4110 on EARN entries; CLAIMS = net debits to 5110; COMM = net debits to 5120; POLICIES = count of ISSUE entries. Product comes from the entry's policy or claim policy. The last closed month is the previous calendar month (`?through=YYYY-MM` overrides it). Earned premium needs the revenue-recognition run (the demo loader does it monthly).

**Analytics (`engine.analysis`)** compares budget with actual by month, quarter or year, with variance, variance %, favourable/adverse direction (`METRIC_GOOD`), prior-year comparison and a full-year outlook. Open months show the projection, never a zero actual.

**Projection methods (`Projector`)**: BUDGET_ADJ (budget x performance factor of the last 6 closed months, capped 0.5-2), SEASONAL (same month last year x recent growth), RUN_RATE (3-month average). History is capped at median + 3 robust deviations (MAD x 1.4826) so a one-off spike does not repeat in the forecast. Closed months always show the true actual. Forecasts cross year-ends; `rolling()` gives a 12-24 month computed forecast and `build_forecast()` stores a full-year FORECAST version (actuals to cutoff, projection after).

**Rollover (`engine.rollover`)**: next-year version from ACTUALS or from another VERSION, with a % uplift and an optional "normalise" tick that caps spikes.

**Outliers (`outliers.py`)**: robust statistics, thresholds adjustable on screen (`z` 3.0, `iqr` 1.5, `min_var` 15 %, `early_days` 30, `late_days` 30). Detectors: variance cells, monthly trend, premium rate (IQR fences by product and vehicle type), claim severity by loss type, early claims, late reporting, repeat claims on one vehicle within 12 months, producer loss ratio and discount vs peers. Findings carry severity, expected value, score, reason and a link to the record.

**Roles**: view = Finance, Production Manager, Underwriter, Claims Manager, Admin; edit = Finance, Production Manager, Admin; approve/lock = Admin, Finance; reopen = Admin; only drafts can be deleted. Approved/locked versions cannot be edited.

**URLs**: `/budget/`, `/budget/new/`, `/budget/<pk>/` (grid), `/budget/<pk>/<action>/`, `/budget-analytics/` (xlsx/csv export), `/budget-forecast/`, `/budget-outliers/`.

**Demo data**: loader creates an approved budget for last and current year, a draft for next year and a stored forecast (all `is_demo`; unload removes them). The budgets deliberately miss in places so variances and outliers show.

**Limitations**: calendar fiscal year; product-level granularity only (no branch/producer budgets); base currency OMR; statistical forecasting, not actuarial reserving; accuracy depends on ledger postings being complete.

## 9b. Reinsurance by treaty, claim reports and broker commission

**Where:** `motor/registers.py` (reports), `motor/reports_ext.py` (premium register), `reinsurance/shares.py` (treaty allocation), `motor/commissions.py` (commission earned / due / settled). Pages are under `/registers/<slug>/`, tabs on every page, CSV and Excel export on all of them.

| Page (slug) | What it shows |
|---|---|
| Premium register (`/reports/premium-register/`) | Existing register plus *Retained premium* and one *Ceded: <treaty>* column per treaty |
| Premium by treaty (`premium-treaty`) | One line per premium transaction x treaty: reinsurer, cession %, sum insured and premium ceded |
| Claim register (`claims-register`) | Claims registered in the period, position at period end; RI recoverable / received / receivable and one column per treaty |
| Claims incurred (`claims-incurred`) | Incurred in the period = paid in period + OS at end - OS at start; RI share per treaty, net incurred, salvage and RI cash; summary by loss type |
| Outstanding as at (`claims-os`) | OS at any date (from the ledger), age bands, RI share per treaty |
| Paid claims (`claims-paid`) | Payments in the period with RI share per treaty, plus reinsurance cash received by treaty |
| Reinsurance by treaty (`ri-treaty`) | Per treaty: premium ceded, claims incurred / paid / OS share, cash received, paid-not-recovered, indicative XoL recovery |
| Broker production & commission (`broker-commission`) | Per broker (or agent): policies, premium, collected, commission earned / not yet due / due / paid / balance, with policy detail |

**All figures come from the IFRS 17 ledger** so they tie to the accounts: incurred = 5110 (claim and payment entries), OS = 2120 as at the date, RI asset = 1230, ceded premium = 2230 per document. The tests assert these reconciliations.

**Treaty split.** Proportional treaties (quota share, surplus): a claim's share under a treaty = premium ceded to that treaty / net premium of the risk (same rule the ledger uses). Premium transactions are split pro-rata to the ceded premium of each treaty on the policy. Facultative placements get their own column (share 0 for claims, as the ledger does not recover them automatically). XoL treaties are shown only as an *indicative* recovery in the treaty summary (retained loss above the retention, capped at the limit, not posted).

**Commission.** `Commission.amount` is the earned amount. A line is *due* in proportion to the premium collected on its policy (clawbacks are deducted in full). `commissions.settle(producer)` pays everything due and not yet paid, creates a `CommissionSettlement`, updates `Commission.settled_amount` and posts Dr 2220 commission payable / Cr 1110 bank (entry type `COMM_PAY`). Finance/Admin pay from the broker page ("Pay what is due"). Brokers and agents can open only the broker page and see only their own business.

**Roles:** Production Manager, Underwriter, Finance, Claims Manager, Claims Handler and Admin see the claim and treaty reports. Only Finance/Admin can pay commission.

**Demo data** now includes reinsurance cash received on about two thirds of settled claims, and one commission settlement per broker/agent for business older than 30 days. Unload removes them.

**Limitations:** treaty shares use the policy's current cessions (an endorsement that re-cedes the policy re-states the split); commission "collected" uses the current collection position, not the position at a past date; XoL recoveries are indicative only.

## 9c. Customers, vehicle profile, loss ratio, report hub, management pack, XoL posting

**Customer search and uniqueness** (`motor/customers.py`). `search(q)` matches every word against name (partial), civil ID / CR number and mobile (digits only, so `+968 9123-4567` finds `91234567`); it returns one row per real person (`identity_key`: ID, CR number, or name+mobile), keeping the record with the most policies. `CustomerForm` and the quote form reject a second customer with the same civil ID / CR. UI: the picker in `static/js/extras.js` (`/api/customer-search/`).

**Vehicle profile** (`motor/vehicles.py`, `views_vehicle.py`). One `Vehicle` row per make|model|year|chassis for life. `VehicleOwnership` keeps the owner history (migration 0006 backfills it). A transfer (the *Transfer of ownership* endorsement, or `vehicles.transfer_directly` when nothing is in force) closes the old ownership and opens a new one; the vehicle row, its policies, claims and history never change. Direct transfer is refused while a policy is in force. `/vehicles/` searches chassis, plate, engine, make/model, colour, owner name/ID/mobile and policy number.

**Loss ratio** (`motor/lossratio.py`, register `loss-ratio`). `compute(user, s, e, group)` with group `insured`, `vehicle` or `both`; written / earned premium and incurred / paid / OS claims come from the ledger by policy, so totals equal the ledger.

**Claim policy -> vehicle**. `claims/api/policy-search/` finds the policy (number, insured name/ID/mobile, plate, chassis); `claims/api/policy/<id>/vehicles/` returns the insured vehicle(s) and drivers; the claim form fills them in on selection.

**Report hub and run-gating** (`motor/reports.py`). `/reports/` shows every report as a card (`CARDS`, filtered by role). No report computes or shows data until `run=1` with a valid `from`/`to` (or `as_at`) is sent (`registers.gate`). Older single-table reports are at `/reports/run/?type=`.

**Management insights** (`motor/mgmt_pack.py`, `mgmt_export.py`, `/management/`). `build(user, s, e)` returns KPIs (with previous equal-length period), findings, monthly trend, product / producer / cause-of-loss tables, treaty table, outliers and budget variance; the page and the PDF, PPTX (native charts), DOCX (trend chart if matplotlib is installed), XLSX (one sheet per table) and CSV exports all render from that one structure. Roles: PRODUCTION_MANAGER, UNDERWRITER, FINANCE, CLAIMS_MANAGER, ADMIN. Add a KPI by adding a dict in `build()`; add an insight rule in `_insights()`.

**XoL calculations and posting**. Premium: each policy accrues `rate % x net premium` to every XoL treaty in force (a `Cession` with `treaty_type=XOL`; Dr 1210 / Cr 2230 with the policy). Deposit instalments: `engine.pay_installment` (Dr 2230 / Cr bank, idempotent). Minimum premium: once the treaty has ended, `engine.post_xol_adjustment` lifts the accrued premium to the minimum (idempotent, source `XOLADJ-<ref>`; the treaty page has a manual button). Claims: `claims.services.xol_recoveries` takes the retained loss after proportional treaties, applies retention and limit per treaty in force on the date of loss, and `sync_claim` posts Dr 1230 / Cr 4210 automatically on every claim movement. Simplifications: each XoL treaty is applied independently (no layering reinstatement); XoL OS/paid split is pro-rata.

## 9d. Claims controls: change log, dual confirmation, locking, garages, closure, treaty recovery, notices, legal, injuries

**Change log** (`core/audit.py`, model `ChangeLog`, screen `/change-log/`). A pre/post-save hook on `TRACKED` models (policy, risk, driver, endorsement, claim, payment, estimate, recovery, subrogation, legal action, injury, notice, guarantee) writes one row per changed field: reference (policy / claim no.), old and new value, user, confirmer, reason. Decimals are compared numerically. Nothing is shown until a period is run; CSV export. A claim's own **History** tab shows its rows. Demo loads run with audit suspended.

**Dual confirmation** (`core/confirm.py`, `PendingAction`, `/confirmations/`, setting `DUAL_CONFIRMATION`). Every claim action (`claims/actions.ACTIONS`) and every policy endorsement is validated, then stored as a pending action (the posted form data). A *different* user with authority (claims: handler / manager / admin; policy: production manager / underwriter / admin) confirms or rejects. On confirm the action is replayed as the maker inside `audit.acting(maker, confirmed_by=checker)`, in a transaction; a failure leaves data untouched and marks the request failed. Set `DUAL_CONFIRMATION = False` to apply changes at once.

**Locking and reopen** (`claims/services.assert_editable`). SETTLED, CLOSED and REJECTED claims are read only. A settled claim still accepts closing work (recoveries, subrogation, invoices, estimate settlement, excess, legal, notices, repair completion if not yet recorded, delay compensation). Closed / rejected only accept reopen, legal and notices. `reopen` needs a reason, returns the claim to APPROVED (or OPEN if rejected) and counts reopenings; `settle` locks it again once fully paid.

**Garage estimates**. No labour hours or hourly rates any more. `RepairEstimate.pricing_basis`: labour only, parts only, labour + parts, or a fixed amount as quoted. A new estimate replaces the garage's earlier one unless marked supplementary. A non-preferred garage more than 15% above the reserve is flagged. Each estimate carries invoice no. / amount / date and a settled flag.

**Closing** (`claims/services.close_blockers`). A claim cannot be closed while payments are outstanding, subrogation is open, any active estimate has no invoice or is unsettled (repair claims also need at least one estimate), or the motor vehicle excess is neither recorded as collected nor waived with a reason. The claim page shows the checklist.

**Treaty recovery on payment** (`services.post_treaty_recovery`, `TreatyRecovery`, register `treaty-recovery`). Each payment (not delay compensation) posts the reinsurers' share to the treaty recovery journal (`TRJ-nnnnnn`), per treaty: proportional treaties by ceded premium / net premium, XoL pro rata to the payment. Cash received from reinsurers (`add_recovery` / closure) settles the oldest balances first. The general ledger already carries the recoverable from the incurred-claim posting (account 1230), so this journal is a sub-ledger and does not change GL totals.

**Subrogation notices** (`SubroNotice`, `services.send_notice`). Registering a claim with an at-fault party and insurer creates the subrogation record and an *opening* notice carrying the damage details (vehicle, circumstances, damage zones, police report, injuries, estimated cost). Completing the repair creates the *repair confirmation* and a *demand notice for payment*. Notices are e-mailed when the insurer's address is on file (otherwise kept for download) and can be re-sent from the Subrogation tab.

**Legal** (`Claim.is_legal`, `LegalAction`). Mark a claim legal on its Legal tab, add actions (demand, filed, hearing, judgment, appeal, execution, settlement) with court, case no., counsel, amounts and next date. `/claims/legal/` lists legal claims.

**Injuries and deaths** (`Injury`, report `injuries`). Record each injured or deceased person with severity, hospital, date of death, compensation and medical cost.

**Automatic estimate** (`services.compute_auto_estimate`). Total cost = vehicle damage (statistical estimate from the damage map, no LLM call) or total-loss value + injuries (entered figure or the indicative `INJURY_COST_TABLE`, with medical) + legal allowance. The reserve follows it until a person overrides it (`estimate_overridden`); "Use the automatic estimate" resets it. `INJURY_COST_TABLE` and `LEGAL_COST_ALLOWANCE` in settings are placeholders: replace them with your tariff / diyya schedule.

## 10. Currency, symbols, UI notes

- `Currency(code, symbol, symbol_fallback, decimals, rate_to_base, is_base, active)`; amounts are rendered by the `money` filter. The new Omani rial symbol is U+20C4; if the browser font lacks the glyph the Arabic abbreviation shown. Drop an official artwork at `static/img/currency/OMR.svg` (any `CODE.svg` there is used automatically). The official glyph is **not bundled**.
- Money inputs (`MoneyInput`) show the symbol of the chosen currency; JS function `MotorPAS.setCurrency(code)`.
- Widgets live in `core/widgets.py` + `templates/widgets/`; `StyledMixin._style()` adds CSS classes to every field.
- JS: `static/js/app.js` (quote form live premium, endorsement field toggling, money symbols, confirm dialogs), `vehicles.js` (vehicle cards, make/model lookup), `extras.js` (customer-policy picker, drivers, damage map, AI estimate, demo progress). Vanilla JS, no build step.
- CSS tokens at the top of `static/css/app.css` (sea-teal / saffron); new component styles are at the end of the file.

---------------------------------------------------------------------

## 11. URL map (main)

| Path | Screen |
|---|---|
| `/` | Dashboard |
| `/quotes/`, `/quotes/new/?customer=<id>`, `/quotes/<id>/` | Quotes; new quote can pre-select a customer |
| `/policies/`, `/policies/<id>/` (+ `share/`, `doc/<key>.pdf`, `endorse/`, `renew/`) | Policies |
| `/customers/`, `/customers/<id>/` | Customers and profile (all policies) |
| `/vouchers/` | Discount vouchers |
| `/payments/`, `/pay/<token>/` (public) | Receipts, online payment |
| `/accounting/`, `/accounting/trial-balance/`, `/accounting/ifrs17/`, `/accounting/documents/` | Accounting |
| `/reports/`, `/reports/premium-register/` | Reports |
| `/claims/`, `/claims/new/`, `/claims/<id>/`, `/claims/ai/`, `/claims/api/estimate/` | Claims and AI |
| `/reinsurance/` | Treaties |
| `/company/`, `/flex-fields/`, `/demo-data/` | Setup (Admin) |
| `/d/<token>/` (public) | Shared documents |
| `/admin/` | Django admin for reference data (currencies, rates, products, limits, commission rules…) |

---------------------------------------------------------------------

- Registers (claims, RI by treaty, broker commission): see 9b (`/registers/<slug>/`).
- Budgeting: see 9a (`/budget/`, `/budget-analytics/`, `/budget-forecast/`, `/budget-outliers/`).

## 12. Testing

`motor/tests_v6.py` (sixth release: change log, dual confirmation, lock / reopen, pricing bases, closure checks, treaty recovery, notices, legal, injuries, automatic estimate).

`motor/tests_registers.py` (treaty registers, run-gated) and `motor/tests_v5.py` (fifth release: customer search / uniqueness, vehicle transfer and search, loss ratio vs ledger, claim vehicle fetch, report gating and hub, management pack and all five exports, XoL accrual / recovery / idempotent posting).

`motor/tests.py` (original workflow, pricing, reinsurance, claims) and `motor/tests_features.py` (second release): demo loader with a small book, ledger balance, every policy has DN+INV, payments never exceed totals, rendering smoke test of ~30 screens, PDFs, premium register CSV/XLSX, `.eml` and WhatsApp links and public share page, customer-policies API, AI estimate with Ollama mocked as down, Ollama model listing, unload only removes demo data, depreciation maths, renewal sum insured, REGNO and ADDDRIVER endorsements, voucher rules, chassis uniqueness, gateway outcomes, over-payment, idempotent revenue recognition, flex-field round trip, Admin-only screens. Tests call `seed_demo` and the loader themselves; mock Ollama with `mock.patch.object(claims.ai, "_request", ...)`.

Add a feature → add a service function → test the function → add a view → add it to the smoke-test URL list.

---------------------------------------------------------------------

## 13. Deployment checklist

1. `DJANGO_DEBUG=0`, strong `DJANGO_SECRET_KEY`, real `DJANGO_ALLOWED_HOSTS`, HTTPS, secure cookies (`SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`).
2. PostgreSQL (`POSTGRES_*`) for multi-user production; SQLite is fine for evaluation. Run `migrate`, then `seed_demo --no-demo` once for reference data and create real users in `/admin/` (assign the role group; set producer type/manager on the profile).
3. Replace illustrative data: rate tables, discounts, fees, authority limits, commission rules, currencies/FX, treaties and providers (admin or `seed_demo.py`).
4. Fill **Company setup** (logo, CR, VAT, bank, public base URL).
5. Static files: `collectstatic` behind nginx/WhiteNoise; media (logos, claim documents) on persistent storage.
6. E-mail backend; cron for `send_renewal_reminders` (daily) and, if wanted, a monthly call of `acc.recognise_revenue`.
7. Payment gateway: implement and configure a real one; remove the demo checkout from public exposure.
8. Ollama on a machine reachable from the app server (GPU recommended for >7B models).
9. Back up DB + media; keep the audit log.
10. Do **not** load demo data in production (it is flagged and removable, but it creates users with the password `demo1234`; delete them with `--remove-users`).

---------------------------------------------------------------------

## 14. Extension points

| Want to… | Do this |
|---|---|
| New product (e.g. Motor Trade) | Add `Product`, covers and `ProductCover`, `RateTable` rows; add treaties/covers mapping; add to seed |
| New endorsement type | Add to `Endorsement.TYPES`; handle in `quote_endorsement()` and `apply_endorsement()`; add fields to `EndorsementForm` and the `map` in `app.js`; update accounting if premium moves |
| New voucher rule | Extend `validate_voucher()` and `rate_risk()` (keep the discountable-base rule) |
| New report | Query in a `reports_ext`-style module, template under `templates/`, add the URL and a menu flag |
| New journal event | Add a function in `accounting/services.py` using `post()`; call it from the service that changes state |
| New flex entity | See §3 Flex fields |
| Another AI backend | Implement `ask_*()` in `claims/ai.py` returning the same JSON keys and branch in `estimate()` |
| Bilingual UI | Wrap templates in `{% trans %}`, enable `LocaleMiddleware`, add RTL CSS; PDFs need an Arabic shaping font |

## 15. Known limitations

- Rates, limits, FX and treaties are placeholders; verify against the current FSA circulars and your approved tariff.
- IFRS 17 is a simplified PAA (see §7); not a substitute for an actuarial model.
- WhatsApp cannot receive attachments from a link; Outlook `.eml` drafts need classic Outlook.
- The demo gateway never moves money; a production gateway needs webhooks and reconciliation.
- Estimates from the AI are advisory; small claim histories give wide ranges.
- Plain Django forms and server rendering by design; there is no REST API yet (views return JSON only for a few AJAX helpers).
- The official OMR symbol glyph is not bundled.
- Treaty recovery journal is a sub-ledger (the GL recoverable is posted on incurred claims); dual confirmation stores the posted form and replays it, so files uploaded with a change are not supported through it.
- XoL: treaties respond independently (no reinstatements or aggregate deductibles); insights thresholds (70% working loss ratio, 40% concentration) are indicative and set in `motor/mgmt_pack.py`.

## 9e. Seventh release: settled lock, estimate reversal on closing, RI waterfall, usage rules, formal schedule

- **Lock.** A SETTLED claim only allows closing, recoveries, subrogation, legal work and notices (`LOCKED_OK`). Invoice, estimate settlement, excess, repair and delay payments need a reopen first. CLOSED claims also accept recoveries (e.g. an overpayment refund).
- **Reversal on closing.** `services.closing_preview` compares the approved cost with the active invoices. If lower, `close_claim(claim, user, confirmed=True)` writes a `ClaimAdjustment` (before / after / delta), sets approved cost, net payable and reserve to the invoice, posts the delta to the ledger (memo on the `sync_claim` entry), and records `Claim.overpaid` plus an OVERPAYMENT recovery when more was already paid. The close form shows a mandatory confirmation checkbox. An invoice above the approved cost is refused: reopen and re-approve. Example: 1000 estimate, 900 invoice: reversed by 100, adjusted to 900.
- **RI waterfall (`services.ri_layers`).** Claim amount = payable after excess minus salvage received. Quota share = amount x QS factor (premium ceded / net premium), capped at `Treaty.qs_limit` (0 = none); then surplus on the remainder; then XoL on what is still retained above the retention, up to its limit. Used by approval, the expected RI recovery, `post_treaty_recovery`, `sync_claim` (1230) and `rebalance_treaty_recoveries` (negative adjustment rows, `payment` is null, netted in `apply_ri_cash`). Salvage receipts trigger a rebalance. The registers still use `shares.claim_shares` (factor view) and tie to the ledger when no limit or salvage applies.
- **Usage rules** (`motor/rules.py`). Bus, truck, trailer and heavy equipment: COMMERCIAL or GOVERNMENT only. GOVERNMENT usage needs `Customer.government_owned`. Enforced in forms, `rate_risk`, `reprice_proposal`, endorsements and the usage picker.
- **Policy schedule** (`motor/pdf.py`): numbered sections (parties, period, vehicles, drivers, covers and limits, premium, excess, endorsements, conditions) and signature block.
- Tests: `motor/tests_v7.py`.

## 9f. Eighth release: credit control, catastrophe events, XoL kinds, ageing, help

- **Credit control** (`motor/credit.py`, models `CreditApproval`, `PolicyInstallment`). The issue view calls `issue_with_terms(proposal, user, basis, method, reference)`: CASH needs a receipt method (reference for cheque / transfer) and records a receipt for the full premium; CREDIT needs an APPROVED `CreditApproval` (1-12 instalments, optional advance, decided by Credit Control / Finance who is not the requester) and records the advance, then `build_schedule` (advance = number 0 due at issue, then every 30 days, last instalment absorbs rounding). `schedule_status` applies receipts oldest first. New role CREDIT_CONTROL (demo login credit1). Switch off with `REQUIRE_PAYMENT_OR_CREDIT = False`; `services.issue_policy` itself is unchanged, so tests and the demo loader are not affected.
- **Catastrophe events**: `claims.CatEvent` (code CAT-yyyy-nn, unique), `Claim.cat_event`, action `cat_flag` (allowed on locked claims), pages `/claims/cat/` and `/claims/cat/<id>/`.
- **XoL kinds** (`Treaty.xol_kind`: RISK, CAT, STOP_LOSS, AGGREGATE; fields aad, aal, sl_attach_pct, sl_limit_pct, reinstatements, reinstatement_pct, premium_basis, bc_*). Only RISK treaties take part in `claims.services.ri_layers`. `reinsurance/xol.py` works on totals: `cat_position(treaty, event)`, `aggregate_position`, `stop_loss_position`, all on `retained_loss(claim)` (after quota share, surplus, per-risk XoL; cat recoveries are deducted before aggregate / stop loss). `post_position` stores an `AggRecovery` and posts only the change (1230 / 4210, journal type XOL_REC so the claim registers still tie); `receive_cash` books cash. `burning_cost(treaty)` returns the yearly table, burning cost rate, indicated rate (loading, min, max) and premium; `xol_apply_rate` creates a new treaty version. Page: `/reinsurance/<id>/xol/`.
- **Ageing** (`motor/aging.py`, register slug `aging`, as-at mode): premium receivable (by instalment), claims receivable (treaty recovery journal, XoL recoveries, subrogation, overpayments), premium payable (XoL deposit instalments, commission on collected premium), claims payable. Buckets: not yet due, 1-30, 31-60, 61-90, 91-180, 181-365, over 365. CSV and Excel exports.
- **Help page** (`core/helpdocs.py`, `/help/`): content as data; `flow()` draws the flow diagrams as inline SVG from node lists, so diagrams cannot drift from the text. Edit the content there.
- Tests: `motor/tests_v8.py`.


## 9g. Ninth release: Property & Casualty (`pnc` app)
**Design.** Separate models (`PncClass`, `PncPolicy`, `PncRisk`, `PncEndorsement`, `PncReceipt`, `PncCredit`, `PncInstallment`, `PncCession`, `PncClaim`, `PncClaimPayment`, `PncRecovery`) on the shared platform: `motor.Customer`, `core.Currency`, `reinsurance.Treaty` (new `business` MOTOR/PNC and `pnc_groups`), `claims.CatEvent`, the ledger and `core.confirm`.
**Modules.** `services.py` rating (`rate_policy`), referral (`approval_reasons`, `submit`, `decide`), credit (`request_credit`, `decide_credit`, `build_schedule`, `schedule_status`), `issue`, `distribute` (quota share, surplus, facultative excess flagged), `endorse` (pro rata on remaining period), claims (`register_claim`, `assess`, `approve`, `pay`, `add_recovery`, `close`, `reopen`) and RI layers (`claim_layers`, `retained_loss`). `ledger.py` posts through `accounting.services.post` with `policy=None` / `claim=None`; entries carry `JournalEntry.book="PNC"` (motor registers, management pack and budgets filter `book="MOTOR"`) and are found by `source` (policy no. or claim no.) and carry the policy's currency and demo flag, so motor registers do not include P&C. `actions.py` queues endorsements and claim reject / close / reopen as `PendingAction(kind="pnc")`.
**Classes.** `classes.py` holds the 16 seed classes (data migration `pnc/0002`; `seed_classes()` is idempotent). Rates are illustrative.
**XoL.** `reinsurance/xol.py` selects claims and subject premium by `treaty.business`; `engine.active_treaties(..., business=)` defaults to MOTOR.
**Known limits.** Facultative excess is flagged, not blocked; P&C XoL premium is by minimum/deposit at treaty level, not accrued per policy; `Cession` and motor registers stay motor-only; aging for P&C is in the P&C reports.
**Tests.** `pnc/tests.py` (19 tests).


## 9h. Tenth release: motor requirements update

- `motor/phase1.py`, `motor/catalogue.py`: renewal window, vehicle conflicts, producer assignment, credit UID/archive/refer-back, PDC, finance interest, make/model seed.
- `claims/heads.py` and `claims/views_heads.py`: `ClaimHead` and `PaymentRelease`. `approve_head` caps at `settings.CLAIM_HEAD_LIMITS`, adds the net to `Claim.net_payable`; `pay_claim` stages only `net_payable` minus approved heads; `disburse` (Accounts) creates a `ClaimPayment` with stage `HEAD`, posts the ledger and the treaty recovery.
- Tests: `motor/tests_v10.py`.


## 9i. Non-motor update (`pnc`)

- `pnc/classes.py`: `EXTRA` fields merged into existing classes by `seed_classes()` (also run by migration `0003_nonmotor`); `NEW_CLASSES` adds CPM, MEDICAL, GMEDICAL.
- `pnc/services.issue(..., fac_reference)` raises `PncError` when a facultative cession exists and `PncPolicy.fac_reference` is empty. `pay()` rejects duplicates.
- Tests: `NonMotorRelease` in `pnc/tests.py`.

- `pnc/nonmotor.py` holds the medical census / pre-authorisation, cargo declarations, reserve authority (`revise_reserve`, `decide_reserve`), reinsurer recoveries (`book_recoveries` is called from `services.pay`; `ri_action` moves the status) and bordereaux. Views: `pnc/views_nm.py`. `Treaty.inuring` (NET / GROSS) is read in `services.claim_layers`. Tests: `NonMotorPhase2`.
