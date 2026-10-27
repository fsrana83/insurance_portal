"""Content of the Help page: every function of the portal, with flow diagrams (inline SVG, drawn from node lists)."""
import textwrap
from html import escape

from django.utils.safestring import mark_safe

KIND = {"step": ("var(--sea-tint)", "var(--sea)"), "gate": ("var(--saffron-tint)", "var(--amber)"), "end": ("var(--green-tint)", "var(--green)"),
        "stop": ("var(--red-tint)", "var(--red)"), "sys": ("var(--blue-tint)", "var(--blue)")}


def flow(nodes, per_row=4, title=""):
    """Draw a left-to-right flow that wraps into rows. ``nodes`` = [(label, kind, note)], kind in step / gate / end / stop / sys."""
    bw, bh, gx, gy, pad = 168, 58, 34, 56, 12
    rows = [nodes[i:i + per_row] for i in range(0, len(nodes), per_row)]
    width = pad * 2 + per_row * bw + (per_row - 1) * gx
    height = pad * 2 + len(rows) * bh + (len(rows) - 1) * gy + (14 if any(n[2] for n in nodes if len(n) > 2) else 0)
    out = [f'<svg class="flow" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title or "Flow diagram")}" xmlns="http://www.w3.org/2000/svg">',
           '<defs><marker id="ah" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0 0L10 5L0 10z" fill="var(--ink-3)"/></marker></defs>']
    pos = []
    for r, row in enumerate(rows):
        for c, n in enumerate(row):
            x, y = pad + c * (bw + gx), pad + r * (bh + gy)
            pos.append((x, y))
            fill, stroke = KIND.get(n[1] if len(n) > 1 else "step", KIND["step"])
            shape = f'<rect x="{x}" y="{y}" width="{bw}" height="{bh}" rx="{bh // 2 if n[1:2] == ("gate",) else 9}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"/>'
            lines = textwrap.wrap(n[0], 24)[:3]
            ty = y + bh / 2 - (len(lines) - 1) * 7 + 4
            text = "".join(f'<text x="{x + bw / 2}" y="{ty + i * 14}" text-anchor="middle" font-size="12" font-weight="600" fill="var(--ink)">{escape(t)}</text>' for i, t in enumerate(lines))
            note = ""
            if len(n) > 2 and n[2]:
                note = f'<text x="{x + bw / 2}" y="{y + bh + 13}" text-anchor="middle" font-size="10.5" fill="var(--ink-3)">{escape(textwrap.shorten(n[2], 34))}</text>'
            out.append(shape + text + note)
    for i in range(len(pos) - 1):
        (x1, y1), (x2, y2) = pos[i], pos[i + 1]
        if y1 == y2:
            out.append(f'<path d="M{x1 + bw} {y1 + bh / 2} H{x2 - 2}" stroke="var(--ink-3)" stroke-width="1.5" fill="none" marker-end="url(#ah)"/>')
        else:
            my = y1 + bh + gy / 2
            out.append(f'<path d="M{x1 + bw / 2} {y1 + bh + 16} V{my} H{x2 + bw / 2} V{y2 - 2}" stroke="var(--ink-3)" stroke-width="1.5" fill="none" marker-end="url(#ah)"/>')
    out.append("</svg>")
    return mark_safe("".join(out))


def S(label, note=""):
    return (label, "step", note)


def G(label, note=""):
    return (label, "gate", note)


def E(label, note=""):
    return (label, "end", note)


def X(label, note=""):
    return (label, "stop", note)


def Y(label, note=""):
    return (label, "sys", note)


def sections():
    return [
        dict(id="start", title="1. Getting started and roles", group="Basics",
             intro=["Advance Motor Insurance Solution issues and services motor policies for the Sultanate of Oman under the unified motor policy and Financial Services Authority (FSA) rules. "
                    "Everything you can see depends on your role. Amounts are shown in the policy currency; reports and the ledger are in Omani rial (OMR, base currency)."],
             table=dict(head=["Role", "What they do"], rows=[
                 ["Agent / Broker", "Quote, convert to proposal, request credit, issue policies, collect receipts, endorse and renew their own business, see their commission."],
                 ["Production Manager / Underwriter", "Approve proposals outside agent authority, manage vouchers and flex fields, endorse; the Underwriter also manages treaties."],
                 ["Credit Control", "Approve or reject credit (instalment) requests; collect receipts; see ageing."],
                 ["Claims Handler", "Register claims, estimates, approvals within limits, payments, notices, legal and injuries."],
                 ["Claims Manager", "Everything a handler does plus approvals above the manager limit and reopening claims."],
                 ["Finance / Reinsurance", "Receipts, accounting, treaty cash, XoL positions, commission settlements, ageing."],
                 ["Customer", "Sees own policies, pays online, requests endorsements."],
                 ["Admin", "All of the above plus setup, demo data and configuration."]]),
             tips=["Every screen is read-only until you press its button; a second person must confirm sensitive changes (section 5).",
                   "Reports show nothing until you choose the period or date and press Run."]),
        dict(id="issue", title="2. From quote to issued policy", group="Policies",
             intro=["A quote is priced from the tariff (own damage and third party rates by vehicle type, usage, engine size and age), then discounts (no-claims, voucher), riders, fees and VAT. "
                    "Pricing is re-run when you convert to a proposal, so the price is always the current tariff."],
             flow=[S("New quote", "customer, vehicles, drivers"), S("Priced quote", "valid for a period"), S("Convert to proposal", "re-rated"), G("Within authority?", "agent limits, risk rules"),
                   S("Underwriter approval", "if referred"), S("Approved", ""), G("Payment terms", "receipt or credit"), E("Policy issued", "schedule emailed")],
             steps=["Create the customer (name, ID or CR, mobile). Search finds existing customers by name, ID card or mobile, so duplicates are avoided.",
                    "Add vehicles: chassis numbers are unique across the system and a vehicle keeps one permanent profile when ownership changes.",
                    "Choose product, cover, riders and sum insured. Convert to proposal; anything outside your authority waits for an approver with the reason shown.",
                    "Once approved, choose how the premium is secured (section 3) and issue. The policy PDF schedule, debit note and VAT invoice are generated and emailed.",
                    "Issuing also distributes the risk to the reinsurance treaties (section 12) and books commission and the IFRS 17 entries (section 14)."],
             tips=["Buses, trucks, trailers and heavy equipment can only be insured for commercial or government use. Government means a government-owned department or company: tick Government-owned on the customer first.",
                   "A risk larger than treaty capacity is issued but held for facultative placement until an underwriter places it."]),
        dict(id="credit", title="3. Payment terms: receipt or credit control", group="Policies",
             intro=["A policy is never issued on a promise. Either the premium is received at issue (cash basis), or Credit Control approves credit with up to 12 monthly instalments and an optional advance."],
             flow=[S("Approved proposal"), G("Cash or credit?"), S("Cash: record receipt", "cash, cheque, transfer, card"), E("Issue policy", "premium fully received"),
                   S("Credit: request", "instalments 1-12, advance"), G("Credit Control decides", "second person"), S("Collect advance", "if any"), E("Issue on credit", "schedule built")],
             steps=["Cash basis: pick how the premium was received and enter the cheque or transfer reference. The receipt voucher is created with the policy.",
                    "Credit basis: the requester gives instalments (1 to 12) and an optional advance. The request appears under Credit control; a different person approves or rejects it (a rejection needs a reason).",
                    "After approval the policy is issued; the advance (if any) is received at issue. The policy page shows an instalment schedule: advance first, then one instalment every 30 days, the last one absorbing rounding.",
                    "Later receipts settle the oldest instalment first. Unpaid instalments show as overdue on the policy and in the receivables ageing report."],
             tips=["Online payment links need a policy, so they are used for instalments after issue, not to issue.",
                   "The rule can be switched off with REQUIRE_PAYMENT_OR_CREDIT in settings (the demo loader does not need it)."]),
        dict(id="servicing", title="4. Endorsements, renewals, cancellation", group="Policies",
             intro=["Mid-term changes are endorsements: change of vehicle details or registration, additional drivers, sum insured, usage, transfer of ownership, cancellation. Each endorsement recalculates premium pro rata, adjusts the treaty cessions and posts the ledger."],
             flow=[S("Request endorsement"), S("Validate", "usage and ownership rules"), G("Second person confirms"), S("Apply", "premium adjusted"), S("Reinsurance and ledger", "re-distributed"), E("Endorsement document")],
             steps=["Renewals copy the policy and re-value the vehicle: the depreciated value is used from the previous sum insured and the vehicle's base date.",
                    "Cancellation refunds unearned premium pro rata and reverses the cession and commission."],
             tips=["The customer can see and request endorsements from their own login; the request still needs confirmation."]),
        dict(id="confirm", title="5. Dual confirmation and the change log", group="Control",
             intro=["Any change to a policy or a claim is a request that a second person confirms. The maker cannot confirm their own request. After confirmation the change is applied by the same code that would run directly, so results are identical."],
             flow=[S("Maker submits change", "PA-number"), Y("Pending confirmation", "shown on the record"), G("Checker reviews"), S("Confirm: applied", "logged"), X("Reject: nothing changes", "reason kept")],
             steps=["Open Confirmations to see requests you may decide. The change log records every field change with old value, new value, who made it, who confirmed it and why.",
                    "Reopening a locked claim needs a reason which is saved in the log."],
             tips=["DUAL_CONFIRMATION in settings switches the second-person step off for tests or single-user installs."]),
        dict(id="receipts", title="6. Receipts and online payments", group="Money",
             intro=["Receipt vouchers are numbered RV/year/number. Cash, cheque, bank transfer and card (POS) are recorded at once. Online payment creates a link and a demo gateway page; the policy is credited when the gateway confirms."],
             steps=["Open a policy, then Receipts and payments to record money or create a payment link. A link can be sent by WhatsApp Desktop or Outlook.",
                    "A receipt posts debit bank / credit receivable and updates the policy status (unpaid, part paid, paid). Commission becomes payable only on premium actually collected."]),
        dict(id="claim-life", title="7. Claim life cycle", group="Claims",
             intro=["A claim is registered against a policy and vehicle. The total cost is estimated automatically from the damage map, injuries and legal allowance; a handler can override it. "
                    "The reserve follows the estimate until it is overridden."],
             flow=[S("Register claim", "damage map, details"), S("Surveyor assigned", "by area and loss type"), S("Garage estimate", "labour, parts, both, fixed"), G("Approve", "within authority"),
                   S("Pay 70% before repair", "FSA rule"), S("Repair completed", "30 day SLA"), S("Pay 30% balance"), E("Settled: read only"), S("Invoices, excess, recoveries"), E("Closed")],
             steps=["Approval sets the approved repair cost, applies the excess and fixes the amount payable. Payments above the manager limit need a Claims Manager.",
                    "Garages quote on one of four bases: labour only, parts only, labour plus parts, or a fixed amount as quoted. Supplementary estimates supersede earlier ones.",
                    "Payments are 70% before repair and 30% on completion; total losses are paid in full. A late repair earns delay compensation.",
                    "Marking a claim settled makes it read only. To change anything (invoice, excess, repair, reserve, estimate) the claim must be reopened with a reason; only closing, incoming recoveries, subrogation, legal work and notices stay available."]),
        dict(id="claim-close", title="8. Closing a claim: reversal of the estimate", group="Claims",
             intro=["A claim cannot close while any garage estimate lacks an invoice, any estimate or invoice is unsettled, or the motor vehicle excess is neither collected nor waived with a reason."],
             flow=[S("Invoices recorded", "per estimate"), S("Estimates settled"), S("Excess collected", "or waived"), G("Invoice vs estimate"), S("Confirm reversal", "tick the box"),
                   S("Adjust to invoice", "logged"), S("RI and ledger follow"), E("Closed")],
             steps=["Example: the approved estimate is 1,000 and the invoice is 900. On closing you are asked to confirm; the estimate is reversed by 100 and the claim adjusted to 900. An Adjustments entry records before, after, who and when, and the ledger entry carries a memo.",
                    "If 1,000 was already paid, 100 is recorded as overpaid and an Overpayment refund recovery is created; when the money comes back record it under Recoveries.",
                    "If the invoice is higher than the approved cost, closing is refused: reopen and re-approve the higher amount first.",
                    "The reinsurers' share is recalculated on the final amount; the treaty recovery journal gets adjustment rows."]),
        dict(id="claim-other", title="9. Subrogation, legal, injuries and the automatic estimate", group="Claims",
             intro=["When another party is at fault, the claim is flagged third-party liable and the at-fault insurer is notified on opening with the damage details. After repair a confirmation and a payment demand notice are created. Recoveries refund the insured's excess first."],
             flow=[S("Open claim: at fault party"), S("Notice to their insurer", "damage details"), S("Pay insured, repair"), S("Confirmation and demand"), S("Record recovery"), E("Excess refunded")],
             steps=["Legal: mark a claim as legal and add legal actions (hearing, judgement, settlement, counsel, cost, next date). The Legal claims page lists them all.",
                    "Injuries and deaths: record each person, severity, hospital, medical cost and compensation reserve; deaths are highlighted. They feed the automatic estimate using the injury cost table.",
                    "Automatic estimate: repair cost from the damage map plus injuries plus legal allowance. Override the reserve any time; Use automatic estimate resets it."]),
        dict(id="cat", title="10. Catastrophe events", group="Claims",
             intro=["For a catastrophe (flood, cyclone, earthquake, fire, sandstorm) mark each affected claim with the event. Choose an existing event or start a new one: it receives a unique code such as CAT-2026-01. "
                    "Claims in one event are added up for the catastrophe excess of loss."],
             flow=[S("Open the claim"), S("Legal / catastrophe tab"), G("Existing event?"), S("New event: name, type, date", "code CAT-yyyy-nn"), S("Claim flagged"), E("Event page: totals and cat XoL")],
             steps=["Catastrophe events lists every event with claims and totals; the event page shows retained loss after quota share, surplus and per-risk XoL, and the recovery under each cat XoL treaty.",
                    "The flag can be changed on settled and closed claims because it only classifies the loss."]),
        dict(id="ri", title="11. Reinsurance program and the claim waterfall", group="Reinsurance",
             intro=["Every policy is distributed to treaties in rank order: quota share (a fixed percentage), surplus (the part of the sum insured above the company retention, up to capacity) and facultative for the excess over capacity. "
                    "Each cession stores the premium ceded. A claim's recovery follows the same sequence."],
             flow=[S("Claim payable", "after excess"), S("Less salvage", "when recovered"), S("Quota share", "factor x amount, up to its limit"), S("Surplus", "factor x amount"), S("Per-risk XoL", "above retention, up to limit"),
                   S("Retained", "company keeps"), S("Cat XoL", "per event total"), E("Aggregate / stop loss", "per period total")],
             steps=["Quota share recovery = claim amount x quota share factor (premium ceded divided by net premium), capped at the treaty's Quota share limit per claim (0 means none).",
                    "Surplus takes its factor of the claim next, then per-risk XoL covers what is still retained above the retention up to its limit. Treaties in force on the date of loss respond.",
                    "Each payment posts the reinsurers' share by treaty to the Treaty recovery journal (TRJ numbers). Cash received from reinsurers settles the oldest balance first.",
                    "Salvage received reduces the amount reinsurers share in; the journal and the ledger are adjusted with negative adjustment rows."]),
        dict(id="xol", title="12. Excess of loss types, burning cost and reinstatements", group="Reinsurance",
             intro=["Excess of loss (XoL) treaties pay only the part of a loss above a retention. Choose the kind when creating the treaty."],
             table=dict(head=["Kind", "Responds to", "Recovery"], rows=[
                 ["Per risk / per occurrence", "One claim", "Retained loss above the retention, up to the limit. Posted claim by claim."],
                 ["Catastrophe XoL", "All claims of one event", "Total retained loss of the event above the retention, up to the limit. Posted per event."],
                 ["Aggregate XoL", "All claims of the treaty year", "Total retained loss above the annual aggregate deductible, up to the annual aggregate limit."],
                 ["Stop loss", "Loss ratio of the treaty year", "Loss ratio above the attachment point, up to the limit, both as a percentage of subject premium (retained net premium)."]]),
             flow=[S("Create XoL treaty", "choose kind"), S("Premium: rate or burning cost"), S("Policies accrue premium"), S("Claims flow through layers"), S("XoL analysis page"), G("Post to ledger"), E("Cash from reinsurer")],
             steps=["Reinstatements: after a loss the limit is restored; each reinstatement costs a percentage of the XoL premium, pro rata to the limit used. The analysis page shows the reinstatement premium for each recovery.",
                    "Burning cost: the layer's losses of each of the last years divided by that year's subject premium give the burning cost rate. The indicated rate is that rate times the loading (for example 1.25), kept between the minimum and maximum rate. Apply indicated rate creates a new treaty version.",
                    "Minimum and deposit premium: the deposit is paid in instalments; at expiry any shortfall of earned premium over the deposit is posted (there is no refund below the deposit).",
                    "Treaties are never edited in place: Modify creates a new version with the reason kept in history."]),
        dict(id="aging", title="13. Receivables and payables ageing", group="Finance",
             intro=["Reports, Receivables / payables ageing, as at a date. Days past the due date are grouped into Not yet due, 1-30, 31-60, 61-90, 91-180, 181-365 and over 365."],
             flow=[S("Choose as-at date"), Y("Run"), S("Summary by section"), S("Detail lines"), E("Export CSV or Excel")],
             table=dict(head=["Section", "Included", "Due date"], rows=[
                 ["Premium receivable", "Unpaid premium of policies; each unpaid instalment on credit policies", "On issue, or the instalment date"],
                 ["Claims receivable", "Reinsurers' share of paid claims, XoL recoveries, subrogation, overpayments", "30 days after posting (60 for subrogation)"],
                 ["Premium payable", "XoL deposit premium instalments, broker commission on collected premium", "Instalment date; 30 days after issue"],
                 ["Claims payable", "Approved claims not yet fully paid", "30 days after the file was finalised (FSA)"]]),
             tips=["All amounts are in OMR. Receipts are counted up to the as-at date, so you can age the position for any past month-end."]),
        dict(id="acct", title="14. Accounting (IFRS 17 view)", group="Finance",
             intro=["Every business event posts a balanced journal. The chart is small and fixed; the Accounting screens list transactions, trial balance, financial documents (debit note, VAT invoice, receipt)."],
             table=dict(head=["Event", "Debit", "Credit"], rows=[
                 ["Policy issued", "Premium receivable", "Unearned premium (and VAT, fees)"],
                 ["Receipt", "Bank", "Premium receivable"],
                 ["Revenue recognised", "Unearned premium", "Insurance revenue"],
                 ["Claim incurred / reserve", "Claims expense", "Liability for incurred claims (LIC)"],
                 ["Claim paid", "LIC", "Bank"],
                 ["Reinsurers' share", "Reinsurance asset", "Reinsurance recoveries"],
                 ["Reinsurance cash", "Bank", "Reinsurance asset"],
                 ["Estimate reversed on closing", "LIC", "Claims expense (memo: reversed to invoice)"]])),
        dict(id="reports", title="15. Reports, management pack and budgets", group="Finance",
             intro=["The Reports hub lists every report as a card: production, new business, renewals, premium register, premium and recoveries by treaty, claim registers, incurred, outstanding as at a date, paid claims, legal claims, injuries, loss ratio by insured or vehicle, broker commission, ageing, change log."],
             steps=["Every report needs a period (or as-at date) and Run. Most export to CSV and Excel. Management insights exports to PDF, PowerPoint, Word, Excel and CSV.",
                    "Budget and forecast: yearly, quarterly and monthly budgets against actual, rollover to the next year, forecast and outlier detection.",
                    "Broker commission: earned, due (on collected premium), settled, balance; Finance settles a broker's due commission."]),
        dict(id="pnc", title="16. Property & Casualty (fire, marine, engineering, liability, accident)", group="P&C",
             intro=["The P&C module is a separate set of screens and records (own quotes, policies, risks, endorsements and claims) that shares customers, currencies, receipts and credit control rules, treaties, catastrophe events, dual confirmation and the ledger with motor. "
                    "Sixteen classes in five groups are set up with an indicative tariff, a minimum premium, a deductible and class-specific questions; edit them in Admin."],
             flow=[S("Quote: class, customer, risks", "rate x sum insured"), G("Within authority?", "referral class, limit, loading"), S("Underwriter approves", "or rejects with reason"), G("Cash or credit?"),
                   E("Issue policy", "receipt or Credit Control approval"), S("Treaties", "quota share, surplus, facultative"), S("Endorse", "pro rata, second person confirms"), S("Claim", "reserve, assess, approve, pay, close")],
             table=dict(head=["Step", "Rule"], rows=[
                 ["Rating", "Premium = sum insured x tariff rate x (1 + loading) x period / 365, never less than the class minimum per risk."],
                 ["Referral", "Referral classes (hull, CAR, EAR, professional indemnity and others), total sum insured above the agent authority, or a loading / discount beyond the limit go to an underwriter."],
                 ["Issue", "A receipt (cash basis) or Credit Control approval (credit, up to 12 instalments, optional advance) is required, as for motor."],
                 ["Reinsurance", "Quota share first, then surplus above the retention up to capacity; any excess is shown as a facultative placement still to be arranged. Per-risk XoL applies to the loss that remains."],
                 ["Claims", "Loss = assessed loss x average factor (sum insured / value at risk when under-insured) less the deductible, capped at the sum insured. Salvage and recoveries reduce the claims expense."],
                 ["Catastrophe", "Flag a P&C claim to a catastrophe event: cat, aggregate and stop-loss XoL use motor and P&C claims separately by treaty business."],
                 ["Ledger", "The same IFRS 17 accounts as motor. P&C postings are found by policy or claim number and appear in the accounting reports and trial balance."]]),
             steps=["Treaties have a Business field (Motor or P&C) and, for P&C, an optional list of class groups they cover.",
                    "P&C reports (premium register, claims register, cessions, receivables aging) are under Property & Casualty > Reports and export to CSV."],
             tips=["Medical policies: open Members and pre-authorisations on the policy. A pre-authorisation earmarks the member's limit and is settled later against the invoice.",
                   "Cargo policies: declare each shipment under Cargo declarations; shipments above the per-conveyance or open cover limit are refused.",
                   "A reserve change needs a reason. Above the handler's authority it waits on the Reserve changes page for the Claims Manager.",
                   "Reinsurance recoveries (Finance or Claims Manager) track each treaty's share of every payment from estimated to collected; bordereaux export to CSV.",
                   "If the sum insured exceeds treaty capacity, enter the signed facultative confirmation reference on the issue form; without it the policy is not issued.",
                   "Medical and Group Medical classes capture member, plan, eligibility and census details; a payment repeated with the same reference, payee and amount is refused.",
                   "Reopening or rejecting a claim, closing it, and every endorsement need a second person to confirm.",
                   "Motor registers do not include P&C business; use the P&C reports."]),
        dict(id="heads", title="17a. Claim cover heads and credit notes", group="Claims",
             intro=["Open a claim and choose Cover heads. Each cover claimed (personal accident, medical, ambulance, towage, third party) has its own estimate, excess, recovery and approval."],
             table=dict(head=["Item", "Rule"], rows=[
                 ["Limits", "Personal accident 15,000, bodily injury or death 100,000, towage 50 (RO). Medical and ambulance have no limit until confirmed. Change them in settings."],
                 ["Own damage", "Still paid 70% before and 30% after repair. Approved heads are added to the amount payable but are not staged."],
                 ["Payment", "Claims raises a credit note for all or part of a head. Accounts disburses it, which records the payment and posts the ledger and reinsurance share."],
                 ["Cash in lieu of repair", "Limited to 80% of the lowest garage quotation (setting), shown on the heads page."]]),
             tips=["Only Accounts can disburse a credit note."]),
        dict(id="admin", title="17. Setup, demo data and glossary", group="Admin",
             intro=["Company setup holds the legal name, registration numbers, logo, bank details and footers printed on every PDF. Flex fields add your own fields to policies, customers and claims. Demo data loads or removes a realistic book of policies, claims, receipts, credit policies, catastrophe events and XoL treaties, all marked demo so they can be unloaded cleanly."],
             table=dict(head=["Term", "Meaning"], rows=[
                 ["Excess", "Amount the insured bears on each claim."], ["LIC", "Liability for incurred claims: the reserve plus approved unpaid amounts."],
                 ["OS", "Outstanding: reserve not yet paid."], ["QS / Surplus", "Proportional treaties."], ["XoL", "Excess of loss treaty."], ["AAD / AAL", "Annual aggregate deductible / limit."],
                 ["Subrogation", "Recovery from the at-fault party."], ["MDP", "Minimum and deposit premium of an XoL treaty."], ["GNRPI", "Gross net retained premium income: the XoL subject premium."],
                 ["FSA", "Financial Services Authority of Oman."]])),
    ]
