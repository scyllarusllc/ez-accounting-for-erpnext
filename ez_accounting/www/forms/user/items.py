# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json
import re

import frappe
from frappe import _
from frappe.utils import cint

from ez_accounting.api import (
	apply_permitted_filter,
	ensure_admin,
	get_companies,
	get_selected_company,
	is_system_manager,
)

no_cache = 1

# The Item Default fields this page manages. Kept as an explicit allowlist
# (rather than trusting whatever fieldname the client sends) since
# update_item_account() does a plain setattr() onto an Item Default row.
# custom_deposit_to_account is the per-item "Deposit to" bank account
# /forms/user/deposit filters receipts by (see ez_accounting.patches.v1_0.
# add_deposit_to_account_field).
ACCOUNT_FIELDS = {"expense_account", "income_account", "custom_deposit_to_account"}

# The only two Item master fields this page's inline edit is allowed to
# touch — kept as an explicit allowlist, same reasoning as ACCOUNT_FIELDS /
# customer.py's EDITABLE_FIELDS.
EDITABLE_FIELDS = {"item_name", "disabled"}


def get_context(context):
	ensure_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "items"
	context.title = _("Item Accounts")
	context.company = get_selected_company()
	context.companies = get_companies()
	# "Show all items" escape hatch is System-Manager-only.
	context.can_show_all = is_system_manager()
	return context


# The company filter's "All companies" sentinel (items.html) — one row per
# Item Default across every company the user may see, each carrying its own
# `company` so the account dropdowns and saves stay per-company.
ALL_COMPANIES = "__all__"


def _resolve_company(company: str) -> str | None:
	company = (company or "").strip()
	if company in ("", ALL_COMPANIES):
		return get_selected_company()
	return company


def _permitted_company_names() -> list[str]:
	return [c.name for c in get_companies()]


def company_item_names(company: str) -> set:
	"""Item names that have an `Item Default` row for `company` — this app's
	definition of "items for this company" (an item's per-company Expense /
	Income / Deposit-to accounts live on that row). Shared with
	ez_accounting.www.forms.user.sales so the Express Sales item picker scopes the
	same way this page's list does."""
	return set(
		frappe.get_all("Item Default", filters={"company": company}, pluck="parent", distinct=True)
	)


@frappe.whitelist()
def get_items_with_accounts(company: str = "", show_all=0):
	"""Items with their Expense/Income/Deposit-to Account for `company`
	(default: the selected company). Disabled items included so this page can
	re-enable them. None means no override — falls back to the Company's own
	generic default account.

	By default only items that already have an Item Default row for `company`
	are listed (this "Item Accounts" page is about per-company accounts).
	`show_all` truthy lists every item, so a newly-added one can be
	configured.

	`company == "__all__"` returns one row per Item Default across every
	company the user may see (each row carries its own `company`) — the
	Administrator's "set up every company's items in one table" view.
	"""
	ensure_admin()

	if company == ALL_COMPANIES:
		return _items_all_companies()

	company = _resolve_company(company)
	if not company:
		return []

	filters = apply_permitted_filter([], "Item")
	# `show_all` honoured only for System Managers.
	if not (int(show_all or 0) and is_system_manager()):
		configured = company_item_names(company)
		if not configured:
			return []
		filters.append(["name", "in", list(configured)])

	items = frappe.get_all(
		"Item",
		filters=filters,
		fields=["name", "item_name", "item_group", "is_sales_item", "is_purchase_item", "disabled"],
		order_by="item_name asc",
	)
	if not items:
		return items

	defaults = frappe.get_all(
		"Item Default",
		filters={"parent": ["in", [i.name for i in items]], "company": company},
		fields=["parent", "expense_account", "income_account", "custom_deposit_to_account"],
	)
	defaults_by_item = {d.parent: d for d in defaults}

	for item in items:
		row = defaults_by_item.get(item.name)
		item["company"] = company
		item["expense_account"] = row.expense_account if row else None
		item["income_account"] = row.income_account if row else None
		item["deposit_to_account"] = row.custom_deposit_to_account if row else None

	return items


def _items_all_companies() -> list[dict]:
	companies = _permitted_company_names()
	if not companies:
		return []

	defaults = frappe.get_all(
		"Item Default",
		filters={"company": ["in", companies]},
		fields=["parent", "company", "expense_account", "income_account", "custom_deposit_to_account"],
	)
	if not defaults:
		return []

	item_filters = apply_permitted_filter(
		[["name", "in", list({d.parent for d in defaults})]], "Item"
	)
	items_meta = {
		i.name: i
		for i in frappe.get_all(
			"Item",
			filters=item_filters,
			fields=["name", "item_name", "item_group", "is_sales_item", "is_purchase_item", "disabled"],
		)
	}

	rows = []
	for d in defaults:
		meta = items_meta.get(d.parent)
		if not meta:
			continue
		rows.append(
			{
				**meta,
				"company": d.company,
				"expense_account": d.expense_account,
				"income_account": d.income_account,
				"deposit_to_account": d.custom_deposit_to_account,
			}
		)
	rows.sort(key=lambda r: ((r.get("item_name") or r["name"]).lower(), r["company"]))
	return rows


def _leaf_accounts(match_field: str, match_value: str, company: str) -> list[dict]:
	"""Leaf accounts of a kind for the company selector's current value.
	Every row carries `company` so the client can pick the right options for
	each row in the "All companies" view. `company == "__all__"` returns the
	kind for every company the user may see."""
	filters = {"is_group": 0, "disabled": 0, match_field: match_value}
	if company == ALL_COMPANIES:
		companies = _permitted_company_names()
		if not companies:
			return []
		filters["company"] = ["in", companies]
	else:
		company = _resolve_company(company)
		if not company:
			return []
		filters["company"] = company
	return frappe.get_all(
		"Account",
		filters=filters,
		fields=["name", "company", "account_number", "account_name"],
		order_by="company asc, name asc",
	)


@frappe.whitelist()
def get_expense_accounts(company: str = ""):
	ensure_admin()
	return _leaf_accounts("root_type", "Expense", company)


@frappe.whitelist()
def get_income_accounts(company: str = ""):
	ensure_admin()
	return _leaf_accounts("root_type", "Income", company)


@frappe.whitelist()
def get_deposit_accounts(company: str = ""):
	"""Bank-type leaf accounts — the options for an item's "Deposit to" (which
	/forms/user/deposit filters receipts by, and deposits into). Same list as
	that page's own Bank Account selector."""
	ensure_admin()
	return _leaf_accounts("account_type", "Bank", company)


def _resolve_default_deposit_account(company: str) -> str | None:
	"""Company default Bank account, falling back to its first Bank ledger."""
	default = frappe.db.get_value("Company", company, "default_bank_account")
	if default and frappe.db.exists(
		"Account", {"name": default, "company": company, "account_type": "Bank", "is_group": 0, "disabled": 0}
	):
		return default
	return frappe.db.get_value(
		"Account",
		{"company": company, "account_type": "Bank", "is_group": 0, "disabled": 0},
		"name",
		order_by="lft asc",
	)


def _resolve_default_item_account(company: str, company_field: str, root_type: str) -> str | None:
	"""Company default Expense/Income account, then first matching leaf."""
	default = frappe.db.get_value("Company", company, company_field)
	if default and frappe.db.exists(
		"Account", {"name": default, "company": company, "root_type": root_type, "is_group": 0, "disabled": 0}
	):
		return default
	return frappe.db.get_value(
		"Account",
		{"company": company, "root_type": root_type, "is_group": 0, "disabled": 0},
		"name",
		order_by="lft asc",
	)


@frappe.whitelist()
def get_item_creation_deposit_accounts(company: str = ""):
	"""Account choices and Company defaults for the Add Item form."""
	ensure_admin()
	company = _resolve_company(company)
	if not company:
		return {
			"accounts": [], "default": None,
			"expense_accounts": [], "default_expense_account": None,
			"income_accounts": [], "default_income_account": None,
		}
	return {
		"accounts": _leaf_accounts("account_type", "Bank", company),
		"default": _resolve_default_deposit_account(company),
		"expense_accounts": _leaf_accounts("root_type", "Expense", company),
		"default_expense_account": _resolve_default_item_account(
			company, "default_expense_account", "Expense"
		),
		"income_accounts": _leaf_accounts("root_type", "Income", company),
		"default_income_account": _resolve_default_item_account(
			company, "default_income_account", "Income"
		),
	}


def _resolve_default_warehouse(company: str) -> str | None:
	"""Best-guess Warehouse for a company's Item Default row. Needed because
	Frappe silently auto-fills a *brand-new* Item Default row's
	default_warehouse from a global sticky default (`tabDefaultValue`,
	defkey="default_warehouse") that has nothing to do with which company the
	row is for — confirmed this bench's global default is "Stores - EIU", left
	over from whichever company was set up first — so appending a row for any
	other company lands a wrong Warehouse on it, and Item.validate() then
	rejects the save with "Warehouse X doesn't belong to Company Y" before the
	admin ever gets to type anything. Prefers the conventional "Stores"
	warehouse for the company (matches ERPNext's own setup-wizard naming),
	falling back to the sole non-group warehouse if there's exactly one — same
	resolution style as ez_accounting.www.forms.user.deposit._resolve_default_account.
	Returns None (left blank, which validates fine) if neither resolves.
	"""
	stores = frappe.db.get_value(
		"Warehouse", {"company": company, "warehouse_name": "Stores", "is_group": 0}, "name"
	)
	if stores:
		return stores

	warehouses = frappe.get_all(
		"Warehouse", filters={"company": company, "is_group": 0}, fields=["name"], order_by="name asc"
	)
	return warehouses[0].name if len(warehouses) == 1 else None


def _ensure_company_item_default(item_doc, company: str | None):
	"""Return the Item Default for ``company``, creating it when missing.

	This keeps Add Item and every edit path consistent: touching an Item from
	the company-scoped Items page also formally assigns it to that company.
	Existing account choices are never overwritten.
	"""
	if not company:
		return None, False
	if company not in _permitted_company_names():
		frappe.throw(_("You are not permitted to use {0}.").format(company), frappe.PermissionError)

	row = next((d for d in item_doc.item_defaults if d.company == company), None)
	created = row is None
	if not row:
		row = item_doc.append(
			"item_defaults",
			{
				"company": company,
				"default_warehouse": _resolve_default_warehouse(company),
				"expense_account": _resolve_default_item_account(
					company, "default_expense_account", "Expense"
				),
				"income_account": _resolve_default_item_account(
					company, "default_income_account", "Income"
				),
				"custom_deposit_to_account": _resolve_default_deposit_account(company),
			},
		)

	warehouse_company = (
		frappe.db.get_value("Warehouse", row.default_warehouse, "company") if row.default_warehouse else None
	)
	if not row.default_warehouse or warehouse_company != company:
		row.default_warehouse = _resolve_default_warehouse(company)
	return row, created


@frappe.whitelist()
def update_item_account(item: str, field: str, account: str = "", company: str = ""):
	"""Set (or, with a blank account, clear) one Item Default account field for
	`company` (default: the selected company). Clearing reverts that item to
	falling back on the Company's generic default account, same as an item
	that was never configured at all.
	"""
	ensure_admin()

	if field not in ACCOUNT_FIELDS:
		frappe.throw(_("Invalid field: {0}").format(field))

	company = _resolve_company(company)
	if not company:
		frappe.throw(_("No Company is configured for this site."))

	if account:
		account_doc = frappe.db.get_value(
			"Account", account, ["name", "company", "is_group", "account_type"], as_dict=True
		)
		if not account_doc or account_doc.is_group or account_doc.company != company:
			frappe.throw(_("{0} is not a valid account for {1}.").format(account, company))
		# "Deposit to" is always a real Bank account — it's where the deposit
		# lands and the key /forms/user/deposit filters receipts by.
		if field == "custom_deposit_to_account" and account_doc.account_type != "Bank":
			frappe.throw(_("Deposit to must be a Bank account. {0} is not.").format(account))

	item_doc = frappe.get_doc("Item", item)
	row, _created = _ensure_company_item_default(item_doc, company)

	row.set(field, account or None)
	item_doc.save()

	return {"item": item_doc.name, "field": field, "account": row.get(field)}


def _norm(text: str | None) -> str:
	return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _guess_account(source_account: str | None, options: list, require_bank: bool = False) -> str | None:
	"""Best guess for the account in the target company that corresponds to
	`source_account`: exact account-number match, then exact (normalised)
	account-name match, then the closest name by similarity ratio (>= 0.6).
	`options` are `_leaf_accounts()` rows (carry account_number / account_name).
	"""
	if not source_account or not options:
		return None
	meta = frappe.db.get_value(
		"Account", source_account, ["account_number", "account_name"], as_dict=True
	)
	if not meta:
		return None

	src_num = (meta.account_number or "").strip()
	if src_num:
		for opt in options:
			if (opt.get("account_number") or "").strip() == src_num:
				return opt["name"]

	src_name = _norm(meta.account_name)
	if src_name:
		for opt in options:
			if _norm(opt.get("account_name")) == src_name:
				return opt["name"]

	from difflib import SequenceMatcher

	best, best_ratio = None, 0.0
	for opt in options:
		ratio = SequenceMatcher(None, src_name, _norm(opt.get("account_name"))).ratio()
		if ratio > best_ratio:
			best, best_ratio = opt["name"], ratio
	return best if best_ratio >= 0.6 else None


def _validate_duplicate_target(target_company: str, source_company: str) -> None:
	if not target_company or not frappe.db.exists("Company", target_company):
		frappe.throw(_("Pick a valid target company."))
	if target_company == source_company:
		frappe.throw(_("The item is already on {0}.").format(target_company))
	if target_company not in _permitted_company_names():
		frappe.throw(_("You are not permitted to use {0}.").format(target_company), frappe.PermissionError)


@frappe.whitelist()
def duplicate_item_preview(item: str, target_company: str, source_company: str = ""):
	"""What the "Duplicate to another company" modal shows: for each of the
	three account fields, the source company's value, a guessed equivalent in
	the target company (see _guess_account), and the full option list to choose
	from. When the target already has an Item Default row, its current values
	are used as the pre-selection instead of a guess (so confirming unchanged
	is a no-op).
	"""
	ensure_admin()

	source_company = _resolve_company(source_company)
	target_company = (target_company or "").strip()
	_validate_duplicate_target(target_company, source_company)

	item_doc = frappe.get_doc("Item", item)
	src = next((d for d in item_doc.item_defaults if d.company == source_company), None)
	tgt = next((d for d in item_doc.item_defaults if d.company == target_company), None)

	specs = [
		("expense_account", _leaf_accounts("root_type", "Expense", target_company), False),
		("income_account", _leaf_accounts("root_type", "Income", target_company), False),
		("custom_deposit_to_account", _leaf_accounts("account_type", "Bank", target_company), True),
	]

	fields = {}
	for field, options, require_bank in specs:
		source_value = src.get(field) if src else None
		if tgt and tgt.get(field):
			guess = tgt.get(field)
		else:
			guess = _guess_account(source_value, options, require_bank)
		fields[field] = {
			"source": source_value,
			"guess": guess,
			"options": [o["name"] for o in options],
		}

	return {
		"item": item_doc.name,
		"item_name": item_doc.item_name or item_doc.name,
		"source_company": source_company,
		"target_company": target_company,
		"target_row_exists": bool(tgt),
		"fields": fields,
	}


@frappe.whitelist()
def duplicate_item_to_company(item: str, target_company: str, accounts: str = "{}", source_company: str = ""):
	"""Create (or update) `item`'s Item Default row for `target_company` with
	the three accounts the admin confirmed in the modal (`accounts` = a JSON
	object keyed by fieldname; "" clears a field / falls back to the Company
	default). Each account is validated against the target company exactly like
	update_item_account does.
	"""
	ensure_admin()

	source_company = _resolve_company(source_company)
	target_company = (target_company or "").strip()
	_validate_duplicate_target(target_company, source_company)

	chosen = json.loads(accounts) if isinstance(accounts, str) else (accounts or {})
	clean: dict[str, str | None] = {}
	for field in ("expense_account", "income_account", "custom_deposit_to_account"):
		account = (chosen.get(field) or "").strip()
		if account:
			meta = frappe.db.get_value(
				"Account", account, ["company", "is_group", "account_type"], as_dict=True
			)
			if not meta or meta.is_group or meta.company != target_company:
				frappe.throw(_("{0} is not a valid account for {1}.").format(account, target_company))
			if field == "custom_deposit_to_account" and meta.account_type != "Bank":
				frappe.throw(_("Deposit to must be a Bank account. {0} is not.").format(account))
		clean[field] = account or None

	item_doc = frappe.get_doc("Item", item)
	row = next((d for d in item_doc.item_defaults if d.company == target_company), None)
	created = row is None
	if not row:
		row = item_doc.append("item_defaults", {"company": target_company})

	# Same defensive warehouse resolution as update_item_account / create_item —
	# a freshly appended row inherits a global sticky default that fails
	# Item.validate()'s warehouse↔company check.
	warehouse_company = (
		frappe.db.get_value("Warehouse", row.default_warehouse, "company") if row.default_warehouse else None
	)
	if not row.default_warehouse or warehouse_company != target_company:
		row.default_warehouse = _resolve_default_warehouse(target_company)

	for field, value in clean.items():
		row.set(field, value)

	item_doc.save()

	return {"item": item_doc.name, "target_company": target_company, "created": created}


@frappe.whitelist()
def update_item_field(item: str, field: str, value: str = "", company: str = ""):
	"""Inline edit of one field on an existing Item — Name or Disabled."""
	ensure_admin()

	if field not in EDITABLE_FIELDS:
		frappe.throw(_("Invalid field: {0}").format(field))

	doc = frappe.get_doc("Item", item)
	company = _resolve_company(company)
	_default_row, default_created = _ensure_company_item_default(doc, company)

	if field == "item_name":
		new_name = (value or "").strip()
		if not new_name:
			frappe.throw(_("Item Name is required."))
		doc.item_name = new_name
	elif field == "disabled":
		doc.disabled = 1 if cint(value) else 0

	doc.save()

	return {
		"item": doc.name,
		"field": field,
		"value": doc.get(field),
		"company": company,
		"item_default_created": default_created,
	}


@frappe.whitelist()
def update_item_details(
	item: str,
	item_name: str,
	item_group: str,
	is_sales_item=0,
	is_purchase_item=0,
	disabled=0,
	company: str = "",
):
	"""Update the Item master fields exposed by the Items page Edit dialog."""
	ensure_admin()
	item_name = (item_name or "").strip()
	if not item_name:
		frappe.throw(_("Item Name is required."))
	if not item_group or not frappe.db.exists("Item Group", {"name": item_group, "is_group": 0}):
		frappe.throw(_("{0} is not a valid Item Group.").format(item_group))

	doc = frappe.get_doc("Item", item)
	company = _resolve_company(company)
	_default_row, default_created = _ensure_company_item_default(doc, company)
	doc.item_name = item_name
	doc.item_group = item_group
	doc.is_sales_item = cint(is_sales_item)
	doc.is_purchase_item = cint(is_purchase_item)
	doc.disabled = cint(disabled)
	doc.save()
	return {
		"item": doc.name,
		"item_name": doc.item_name,
		"item_group": doc.item_group,
		"is_sales_item": doc.is_sales_item,
		"is_purchase_item": doc.is_purchase_item,
		"disabled": doc.disabled,
		"company": company,
		"item_default_created": default_created,
	}


@frappe.whitelist()
def delete_item(item: str):
	"""Delete an Item outright — only actually succeeds if nothing references
	it (Frappe's own link-check, same protection Desk's delete uses); a
	linked item (used on any Sales/Purchase Invoice, Item Default, etc.)
	raises LinkExistsError with the specific linked records listed, surfaced
	to the admin as-is rather than caught and reworded. Disabling (via
	update_item_field) is the safe default for an item that's just no longer
	wanted going forward but has real history — delete is only for a
	genuinely unused/mistaken entry.
	"""
	ensure_admin()

	frappe.delete_doc("Item", item)
	frappe.db.commit()

	return {"deleted": item}


@frappe.whitelist()
def get_item_groups():
	ensure_admin()
	return frappe.get_all(
		"Item Group",
		filters={"is_group": 0},
		fields=["name"],
		order_by="name asc",
	)


@frappe.whitelist()
def create_item(
	item_name: str,
	item_group: str,
	is_sales_item=1,
	is_purchase_item=1,
	company: str = "",
	deposit_to_account: str = "",
	expense_account: str = "",
	income_account: str = "",
):
	"""Add a new Item from this page. item_code is derived from item_name
	directly (this site's Stock Settings has "Item Naming By" = Item Code, so
	the docname/item_code IS the human-readable code) — one plain name field,
	same as this app's other Add-record forms (Customer/Supplier), rather
	than asking for a separate code up front. Always non-stock
	(is_stock_item=0) — nothing on this site tracks physical inventory; every
	item seen across Express Purchase/Sales is a fee/donation/service line.

	The new item gets an `Item Default` row for `company`, including the chosen
	Deposit-to Bank account, right
	away, so it immediately shows up in this page's company-scoped list — a
	scoped user (no "Show all") would otherwise create an item and never see
	it again, since the list only shows items with an Item Default row for the
	selected company.
	"""
	ensure_admin()

	item_name = (item_name or "").strip()
	if not item_name:
		frappe.throw(_("Item Name is required."))

	if not item_group or not frappe.db.exists("Item Group", {"name": item_group, "is_group": 0}):
		frappe.throw(_("{0} is not a valid Item Group.").format(item_group))

	if frappe.db.exists("Item", item_name):
		frappe.throw(_('An item named "{0}" already exists.').format(item_name))

	company = _resolve_company(company)
	deposit_to_account = (deposit_to_account or "").strip()
	if company and not deposit_to_account:
		deposit_to_account = _resolve_default_deposit_account(company) or ""
	if company and deposit_to_account and not frappe.db.exists(
		"Account",
		{
			"name": deposit_to_account,
			"company": company,
			"account_type": "Bank",
			"is_group": 0,
			"disabled": 0,
		},
	):
		frappe.throw(_("Deposit to must be an active Bank account for {0}.").format(company))

	account_values = {
		"expense_account": ((expense_account or "").strip(), "Expense", "default_expense_account"),
		"income_account": ((income_account or "").strip(), "Income", "default_income_account"),
	}
	resolved_accounts = {}
	for fieldname, (account, root_type, company_field) in account_values.items():
		if company and not account:
			account = _resolve_default_item_account(company, company_field, root_type) or ""
		if company and account and not frappe.db.exists(
			"Account",
			{"name": account, "company": company, "root_type": root_type, "is_group": 0, "disabled": 0},
		):
			frappe.throw(_("{0} must be an active {1} account for {2}.").format(fieldname, root_type, company))
		resolved_accounts[fieldname] = account

	doc = frappe.get_doc(
		{
			"doctype": "Item",
			"item_code": item_name,
			"item_name": item_name,
			"item_group": item_group,
			"stock_uom": frappe.db.get_single_value("Stock Settings", "stock_uom") or "Nos",
			"is_stock_item": 0,
			"is_sales_item": cint(is_sales_item),
			"is_purchase_item": cint(is_purchase_item),
		}
	)
	if company:
		# default_warehouse resolved the same defensive way update_item_account
		# does — a bare appended row otherwise inherits a global sticky default
		# that fails Item.validate()'s warehouse↔company check.
		doc.append(
			"item_defaults",
			{
				"company": company,
				"default_warehouse": _resolve_default_warehouse(company),
				"custom_deposit_to_account": deposit_to_account or None,
				"expense_account": resolved_accounts["expense_account"] or None,
				"income_account": resolved_accounts["income_account"] or None,
			},
		)
	doc.insert()

	return {
		"item": doc.name,
		"item_name": doc.item_name,
		"item_group": doc.item_group,
		"is_sales_item": doc.is_sales_item,
		"is_purchase_item": doc.is_purchase_item,
		"deposit_to_account": deposit_to_account or None,
		"expense_account": resolved_accounts["expense_account"] or None,
		"income_account": resolved_accounts["income_account"] or None,
		"disabled": doc.disabled,
	}
