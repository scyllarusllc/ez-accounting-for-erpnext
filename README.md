# EZ Accounting for ERPNext

A full accounting portal built on top of [ERPNext](https://erpnext.com) and the [Frappe Framework](https://frappeframework.com) — a simplified, guided front end for the day-to-day workflows non-accounting staff actually touch, backed by ERPNext's own accounting engine underneath.

ERPNext already provides a complete accounting engine — Sales Invoices, Payment Entries, GL Entries, bank accounts. **EZ Accounting doesn't replace any of that.** It never modifies ERPNext core; every page reads and writes standard ERPNext documents through the normal API, so upgrades and maintenance stay simple.

> Grown out of a real production accounting portal, open-sourced so other ERPNext users (and their developers) can use and extend it.

## Features

**Sales & Payments**
- **Express Sales** — a single guided flow for Sales Invoice + Payment: full payment, partial payment, split payment (cash/check/card/bank in one transaction), or invoice-only.
- **Deposit Management** — group undeposited cash and checks into a single bank deposit.
- **Bank Reconciliation** — match bank statement lines against ERPNext transactions.

**Payroll**
- **FICA report** — Social Security/Medicare payable, company-configurable.
- **Treasurer report** — withholding tax payable.
- **Payroll Summary**, built on ERPNext/HRMS Salary Slips.

**Reporting**
- Aged Receivables / Aged Payables
- Cash Receipts / Cash Disbursements Journals
- Check Register, Customer Ledgers, Vendor Ledgers, Vendor List
- Sales Journal, Purchase Journal, 1099 Vendor Report

**Operations**
- Document Finder (company-scoped file/document search)
- Company Documents & procedures manual
- Customizable print formats and letterheads
- AI-assisted inbox reading for emailed documents (optional)
- Per-user page access control (`Forms User Access`) on top of standard Frappe roles

## Architecture

```
┌────────────────┐
│  Express Sales │   guided UI: pick customer → items → payment
└───────┬────────┘
        ↓
┌────────────────┐
│ Sales Invoice   │   standard ERPNext document
└───────┬────────┘
        ↓
┌────────────────┐
│ Payment Entry   │   standard ERPNext document
└───────┬────────┘
        ↓
┌─────────────────────┐
│ Undeposited Funds    │   holding account until physically deposited
└──────────┬───────────┘
           ↓
┌────────────────┐
│ Bank Deposit    │   groups entries into one Journal Entry
└───────┬────────┘
        ↓
┌────────────────────┐
│ Bank Reconciliation │   match deposit against bank statement line
└────────────────────┘
```

## Tech stack

Python · Frappe Framework · ERPNext · JavaScript · MariaDB · REST API · Docker

## Installation

```bash
bench get-app ez_accounting https://github.com/scyllarusllc/ez-accounting-for-erpnext
bench --site <site-name> install-app ez_accounting
```

## License

[MIT](LICENSE)
