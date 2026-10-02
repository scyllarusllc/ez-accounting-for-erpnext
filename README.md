# EZ Accounting for ERPNext

Practical accounting workflow extensions for [ERPNext](https://erpnext.com) and the [Frappe Framework](https://frappeframework.com).

ERPNext already provides a complete accounting engine — Sales Invoices, Payment Entries, GL Entries, bank accounts. **EZ Accounting doesn't replace any of that.** It sits on top of it and simplifies the day-to-day workflows that non-accounting staff actually touch: taking a payment, depositing cash/checks, and reconciling the bank statement.

> Built from real-world accounting operations and generalized into an open-source Frappe app.

## Features

- **Express Sales** — a single guided flow for Sales Invoice + Payment, supporting full payment, partial payment, split payment (cash/check/card/bank in one transaction), and invoice-only.
- **Deposit Management** — group undeposited cash and checks into a single bank deposit, matching ERPNext's `Mode of Payment` and `Bank Account` records.
- **Bank Reconciliation** — a simple matching screen: bank transactions on one side, ERPNext transactions on the other, with match / unmatch / reconcile actions.
- **Accounting Dashboard** — today's sales, payments received, undeposited funds, outstanding AR, at a glance.

## Architecture

EZ Accounting is a standalone Frappe app. It never modifies ERPNext core — it reads and writes standard ERPNext documents (Sales Invoice, Payment Entry, GL Entry) through the normal API, which keeps upgrades and maintenance simple.

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
│ Bank Deposit    │   EZ Accounting groups entries into one deposit
└───────┬────────┘
        ↓
┌────────────────────┐
│ Bank Reconciliation │   match deposit against bank statement line
└────────────────────┘
```

## Tech stack

Python · Frappe Framework · ERPNext · JavaScript · MariaDB · REST API · Docker

## Status

Early stage — Express Sales and Deposit Management are functional; Reconciliation and the Dashboard are in progress. See [docs/roadmap.md](docs/roadmap.md).

## Installation

```bash
bench get-app ez_accounting https://github.com/<your-username>/ez-accounting-for-erpnext
bench --site <site-name> install-app ez_accounting
```

## License

[MIT](LICENSE)
