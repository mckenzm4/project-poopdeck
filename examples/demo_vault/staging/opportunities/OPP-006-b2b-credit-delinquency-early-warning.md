---
id: OPP-006
title: B2B Customer Credit Delinquency & Bad-Debt Early Warning Model
status: staging
boss_mandate: false
domain_pack: finance_risk
created_at: &id001 2026-11-11
updated_at: *id001
internal_datasets:
- ref: '[[prod_catalog.gold.otif_shipments]]'
  role: customer shipment cadence and disputed SLA chargeback history
  transformations_needed:
  - 'Aggregate customer dispute velocity (note: lacks true historical AR aging snapshots)'
- ref: '[[hive_metastore.legacy_erp.bronze_freight_invoices]]'
  role: ancillary freight billing disputes
  transformations_needed:
  - Link shipment disputes to customer accounts
external_data_required:
- source: FRED Macroeconomic Series (CPI, Industrial Production, Interest Rates, Freight
    TSI)
  purpose: Control for high-yield credit spread and commercial borrowing rate shifts
  acquisition: free_public_api
  blocker_severity: low
- source: Dun & Bradstreet (D&B) B2B Firmographics, PAYDEX & Delinquency Scores
  purpose: Supply external commercial credit stress and PAYDEX delinquency scores
  acquisition: net_new_budget
  blocker_severity: high
existing_reports_supplemented:
- ref: '[[Freight & Working Capital Finance Tracker]]'
  integration_mode: Replace backward-looking `Customer Credit Risk Bucket` DAX aging
    tier
new_reports_unlocked: []
valuation:
  confidence_tier: hypothesis
  driver_formula: annual_bad_debt_writeoff_usd * addressable_accounts_share * prevented_writeoff_pct
  drivers:
  - name: annual_bad_debt_writeoff_usd
    value: 950000.0
    range: null
    source: Unverified AR discussion estimate
    verified: false
    verify_question: Confirm with [[Elena Rostova]] actual trailing 12M net bad-debt
      write-offs excluding disputed OTIF deductions.
  - name: addressable_accounts_share
    value: 0.6
    range: null
    source: Credit-hold policy hypothesis
    verified: false
    verify_question: Confirm with [[Elena Rostova]] whether Commercial Sales allows
      automated pre-shipment credit holds.
  - name: prevented_writeoff_pct
    value: null
    range:
    - 0.1
    - 0.18
    - 0.28
    source: Vendor whitepaper estimate
    verified: false
    verify_question: Test whether a 20-line SQL/DAX 14-day payment-velocity slope
      in [[Freight & Working Capital Finance Tracker]] captures 80% of delinquency
      alerts.
  annual_benefit_low_usd: 57000.0
  annual_benefit_base_usd: 102600.0
  annual_benefit_high_usd: 159600.0
  verification_prompts:
  - '[VERIFY WITH STAKEHOLDER] annual_bad_debt_writeoff_usd: Confirm with [[Elena
    Rostova]] actual trailing 12M net bad-debt write-offs excluding disputed OTIF
    deductions.'
  - '[VERIFY WITH STAKEHOLDER] addressable_accounts_share: Confirm with [[Elena Rostova]]
    whether Commercial Sales allows automated pre-shipment credit holds.'
  - '[VERIFY WITH STAKEHOLDER] prevented_writeoff_pct: Test whether a 20-line SQL/DAX
    14-day payment-velocity slope in [[Freight & Working Capital Finance Tracker]]
    captures 80% of delinquency alerts.'
effort_weeks: 4.5
data_readiness_score: 2
adoption_friction_score: 3
computed_priority_score: 17.05
steps_to_completion:
- Request historical daily AR aging snapshot export from ERP Data Engineering (`Q-DE-02`)
- Prototype a deterministic 20-line SQL/DAX payment-velocity alert with [[Priya Nair]]
  before any ML build
- Evaluate whether net-new Dun & Bradstreet procurement (`Q-MGR-04`) is justified
  after SQL baseline
stakeholders:
- '[[Elena Rostova]]'
- '[[Priya Nair]]'
cfo_critique_verdict: SIMPLIFY_TO_SQL_DAX
cfo_critique_notes:
- 'Target & Snapshot Leakage Risk: `vault/datasets/` currently lacks point-in-time
  historical AR aging snapshots — training on current-state ERP balances would leak
  future collections.'
- 'High Procurement Blocker: Requires net-new Dun & Bradstreet budget (`blocker_severity:
  high`, `Q-MGR-04`).'
- '20-Line SQL/DAX Challenge (`SIMPLIFY_TO_SQL_DAX`): A deterministic SQL/DAX measure
  tracking 30-day vs. 90-day days-to-pay drift in [[Freight & Working Capital Finance
  Tracker]] solves 80% of this problem in 2 days with zero vendor spend.'
---

# OPP-006: B2B Customer Credit Delinquency & Bad-Debt Early Warning Model

- **Status**: `staging` (ROI-Ranked)
- **Domain Pack**: `finance_risk`
- **Confidence Tier**: `hypothesis`
- **Effort**: `4.5` person-weeks | **Priority Score**: `17.05`
- **Annual Benefit Range (Low / Base / High)**: `$57,000` / `$102,600` / `$159,600`

## Data & Engineering Footprint
- **Internal**: [[prod_catalog.gold.otif_shipments]] (customer shipment cadence and disputed SLA chargeback history) — _Aggregate customer dispute velocity (note: lacks true historical AR aging snapshots)_
- **Internal**: [[hive_metastore.legacy_erp.bronze_freight_invoices]] (ancillary freight billing disputes) — _Link shipment disputes to customer accounts_
- **External**: **FRED Macroeconomic Series (CPI, Industrial Production, Interest Rates, Freight TSI)** (`free_public_api`, blocker: `low`) — Control for high-yield credit spread and commercial borrowing rate shifts
- **External**: **Dun & Bradstreet (D&B) B2B Firmographics, PAYDEX & Delinquency Scores** (`net_new_budget`, blocker: `high`) — Supply external commercial credit stress and PAYDEX delinquency scores

## Power BI Delivery Surface
- **Supplements Existing Report**: [[Freight & Working Capital Finance Tracker]] — Replace backward-looking `Customer Credit Risk Bucket` DAX aging tier

## Driver-Tree Valuation & Verification Prompts

**Formula**: `annual_bad_debt_writeoff_usd * addressable_accounts_share * prevented_writeoff_pct`

| Driver | Value / Range | Verified | Source |
|---|---|---|---|
| `annual_bad_debt_writeoff_usd` | `950000` | No (`[VERIFY]`) | Unverified AR discussion estimate |
| `addressable_accounts_share` | `0.6` | No (`[VERIFY]`) | Credit-hold policy hypothesis |
| `prevented_writeoff_pct` | `[0.1, 0.18, 0.28]` | No (`[VERIFY]`) | Vendor whitepaper estimate |

### Required Stakeholder Verifications
- [VERIFY WITH STAKEHOLDER] annual_bad_debt_writeoff_usd: Confirm with [[Elena Rostova]] actual trailing 12M net bad-debt write-offs excluding disputed OTIF deductions.
- [VERIFY WITH STAKEHOLDER] addressable_accounts_share: Confirm with [[Elena Rostova]] whether Commercial Sales allows automated pre-shipment credit holds.
- [VERIFY WITH STAKEHOLDER] prevented_writeoff_pct: Test whether a 20-line SQL/DAX 14-day payment-velocity slope in [[Freight & Working Capital Finance Tracker]] captures 80% of delinquency alerts.

## Steps to Completion
1. Request historical daily AR aging snapshot export from ERP Data Engineering (`Q-DE-02`)
2. Prototype a deterministic 20-line SQL/DAX payment-velocity alert with [[Priya Nair]] before any ML build
3. Evaluate whether net-new Dun & Bradstreet procurement (`Q-MGR-04`) is justified after SQL baseline

## Skeptical CFO Critique
- **Verdict**: `SIMPLIFY_TO_SQL_DAX`
- Target & Snapshot Leakage Risk: `vault/datasets/` currently lacks point-in-time historical AR aging snapshots — training on current-state ERP balances would leak future collections.
- High Procurement Blocker: Requires net-new Dun & Bradstreet budget (`blocker_severity: high`, `Q-MGR-04`).
- 20-Line SQL/DAX Challenge (`SIMPLIFY_TO_SQL_DAX`): A deterministic SQL/DAX measure tracking 30-day vs. 90-day days-to-pay drift in [[Freight & Working Capital Finance Tracker]] solves 80% of this problem in 2 days with zero vendor spend.

Red-Team Gate Summary: Do NOT promote as an ML build in Q1. Recommend archiving or simplifying to a 20-line SQL/DAX payment-velocity alert until historical AR snapshots exist.

<!-- HUMAN-NOTES-START -->
## Human Notes
- Good candidate to archive or convert into a 2-day BI enhancement during `/review-staging`.
<!-- HUMAN-NOTES-END -->
