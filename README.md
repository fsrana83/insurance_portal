# Advance Motor Insurance Solution (Oman / FSA)

Django motor policy issuance, claims, reinsurance and IFRS 17 accounting system. Full documentation: **docs/DEVELOPER_GUIDE.md**.

## Run
    pip install -r requirements.txt
    python manage.py migrate
    python manage.py seed_demo                 # reference data + demo logins (use --no-demo for reference data only)
    python manage.py runserver

Demo logins (password `demo1234`): admin, pm1, uw1, agent1, broker1, claims1, claimsmgr1, finance1, customer1.

## Demo data (1,000 policies / 300 claims)
    python manage.py load_demo_data            # --policies 1000 --claims 300 --seed 42 (about 90 seconds)
    python manage.py unload_demo_data          # removes only records flagged is_demo (--remove-users also drops demo logins)
or sign in as admin and open **Demo data** in the menu (runs in the background with a progress bar).

## What is in the box
- Quote > proposal > approval > issuance, fleet upload, renewals (depreciated sum insured), endorsements (incl. registration-number change, additional driver), cancellations.
- Customer profile with all policies; pick a policy to renew or issue a new one. Additional drivers per vehicle.
- Discount vouchers (percent / fixed, discountable part only), admin-defined flex fields, company setup (letterhead data).
- Policy schedule, debit note, credit note, VAT tax invoice and receipt PDFs; premium register (screen / CSV / Excel).
- Receipt vouchers and online payment requests with a demo gateway; WhatsApp Desktop and Outlook (.eml) sending.
- IFRS 17 (PAA) journals, trial balance, reconciliations and insurance service result.
- Claims with an interactive vehicle damage map, AI repair-cost estimate (local Ollama + your claims history), total-loss value by reducing-balance depreciation.
- Treaty engine (QS, surplus, XOL, facultative), commissions, providers, dashboards, audit trail.
- OMR symbol (U+20C4), multi-currency issuance; FSA-regulated TPL tariff rules.

- **Budgeting & forecasting**: budget versions with approval, budget vs actual (monthly/quarterly/annual), rollover, rolling forecast, outlier detection. See guide section 9a.

- **Reinsurance by treaty and claim reports**: premium and claim registers split by treaty, claims incurred (period), outstanding as at a date, paid claims, reinsurance recoveries, broker production with commission earned / due / paid. See guide section 9b.

- **Customers, vehicles, loss ratio, management pack, XoL**: unique customer search (name, ID / CR, mobile); permanent vehicle profile with ownership history and transfer; vehicle-level search; loss ratio by insured / vehicle / both; claims pick a policy and fetch its vehicle; all reports as cards and empty until a period is run; management insights with PDF / PPTX / DOCX / XLSX / CSV export (`/management/`); XoL premium, instalments, minimum top-up and claim recoveries post automatically. See guide section 9c.

- **Claims controls**: field-level change log; dual confirmation (maker-checker) for every policy / claim change; settled claims read only until reopened; garage estimates by labour only / parts only / labour + parts / fixed quote with invoice and settlement; closure blocked until estimates are invoiced and settled and the excess is recorded; reinsurers' share posted by treaty on every payment; notices to the at-fault insurer (opening, repair confirmation, demand); legal actions; injuries and deaths; automatic total-cost estimate you can override. See guide section 9d.

## Scheduled job
    python manage.py send_renewal_reminders    # daily: T-30/T-7/T-0 reminders and marks lapsed policies EXPIRED

## Tests
    python manage.py test

## Release 7 highlights
Settled claims are read only until reopened; closing reverses the estimate to the invoiced amount (with confirmation and a logged adjustment); reinsurance recovery follows quota share (to its limit), then surplus, then XoL; formal policy schedule PDF; buses, trucks, trailers and heavy equipment are commercial or government only (government = government-owned department or company).

## Release 8 highlights
Receipt or Credit Control approval to issue (up to 12 instalments, optional advance); catastrophe events with unique codes; per-risk, catastrophe, aggregate and stop-loss XoL with reinstatements and burning-cost pricing; receivables / payables ageing for premiums and claims; Help and guide page with flow diagrams. Demo login: credit1 / demo1234.

## Release 10b highlights: non-motor requirements update

- New classes: Contractors' Plant & Machinery, Medical and Group Medical (new Medical group), with the line-specific questions from the requirements (geocode and valuation basis for property, Incoterms and transshipment for cargo, vessel particulars for hull, claims-made basis and retroactive date for liability, member and census fields for medical). Existing set-ups gain the new questions without losing edits.
- **Facultative control:** when the sum insured exceeds treaty capacity, the policy cannot be issued until the signed facultative confirmation reference is entered (setting `REQUIRE_FAC_CONFIRMATION`, default on).
- **Duplicate payments:** a claim payment with the same reference, payee and amount is refused.
- Tests reproduce the document's calculation examples (excess of loss layers, stop loss, surplus split).
- **Medical:** member census (one by one or CSV upload with row-wise errors, duplicate and overlapping enrolment blocked), removal with effective date, pre-authorisation that earmarks the member's benefit limit (eligibility on the service date, waiting period, remaining limit), final settlement against the actual invoice with duplicate-invoice protection.
- **Cargo open cover:** shipment declarations with per-conveyance and open-cover limit checks, premium at the policy rate, duplicate invoice check, cancellation.
- **Reserve authority:** every P&C reserve change needs a reason and is logged. Changes above the handler's authority (`MOTOR_PNC_RESERVE_AUTH`, default 20,000 base) wait for a Claims Manager other than the requester (Reserve changes page).
- **Reinsurer recoveries:** each claim payment books every treaty's share as a recoverable that moves Estimated, Agreed, Billed, Disputed, Collected (partial collections post the ledger). Premium and claims bordereaux export to CSV from the Reinsurance recoveries page.
- **Inuring order:** each per-risk XoL treaty has an inuring basis (applies after proportional recoveries, or to the loss before them); total recovery never exceeds the loss.

## Release 10 highlights: motor requirements update

- **Policy and customer:** fuel type on vehicles, CR registration and expiry dates for companies, vehicle make/model catalogue, renewal window check, vehicle overlap warnings, producer (agent/broker) assignment with history, PA rider limited to TPL.
- **Credit control:** every credit request gets a unique UID, a refused request is archived and a new one can be raised, Credit Control can refer a request back, post-dated cheques and vehicle finance (financier interest) records.
- **Claim heads:** each cover claimed (personal accident, medical, ambulance, towage, third party) has its own estimate, excess, recovery and approval, with limits from settings (PA 15,000, bodily injury 100,000, towage 50; medical and ambulance unlimited until confirmed). Approved heads add to the claim's net payable. Payment is by a credit note raised by Claims and disbursed by Accounts, in parts if needed. The 70/30 repair staging applies to own damage only.
- **Cash in lieu of repair** is capped at a configurable share (default 80%) of the lowest garage quotation, shown on the heads page.
- Settings that the requirements document marks "to be confirmed": `CLAIM_HEAD_LIMITS`, `CASH_SETTLEMENT_PCT` (environment variables `MOTOR_LIMIT_*`, `MOTOR_CASH_PCT`).
- **Not yet built:** garage / surveyor portals, IT Admin approval for self-registration, vendor invoices, fleet transfer endorsements, the legal-dispute and falsified-claim investigation workflows.

## Release 9 highlights: Property & Casualty module
New `pnc` app on the shared platform (menu: Property & Casualty). Sixteen classes in five groups (fire & property, marine, engineering, liability, miscellaneous accident) with tariff, minimum premium, deductible, referral flag, average clause and class-specific risk questions (Admin > P&C classes). Quote with several risks, underwriter referral, issue on receipt or Credit Control approval (up to 12 instalments, optional advance), schedule PDF, endorsements (sum insured, add / remove risk, extension, details, cancellation) pro rata with second-person confirmation, claims (reserve, loss adjuster, assessment with average and deductible, approval limit, payments, salvage / subrogation / reinsurance recoveries, close, reopen, catastrophe flag), quota share / surplus / facultative cessions, per-risk, cat, aggregate and stop-loss XoL on P&C claims (treaties have a Business field), IFRS 17 ledger postings, P&C reports (premium, claims, cessions, aging) and demo data (45 policies, 14 claims). Help page section 16 explains the flow.
