# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import add_days, add_to_date, flt, get_first_day, get_first_day_of_week, nowdate

from ez_accounting.api import (
	ensure_admin,
	ensure_logged_in,
	get_dashboard_companies,
	get_online_users,
	is_admin_user,
)
from ez_accounting.website_context import GL_LINKS_SITES

no_cache = 1

# Business doctypes worth surfacing in the "who deleted anything" feed --
# deliberately an allowlist, not every deletion Frappe logs: things like
# File/Communication/Version/Notification Log rows disappear constantly as a
# side effect of completely routine use and would drown out anything that
# actually matters to an admin skimming this panel.
ACTIVITY_DELETION_DOCTYPES = [
	"Sales Invoice",
	"Purchase Invoice",
	"Payment Entry",
	"Journal Entry",
	"Customer",
	"Supplier",
	"Employee",
	"Salary Slip",
	"Payroll Entry",
	"Forms Company Document",
	"Forms Bank Deposit Sheet",
	"Item",
	"Company",
]


# How far back the merged Activity Feed looks, regardless of how many rows
# that turns out to be (each source is separately capped, see get_activity()).
ACTIVITY_FEED_WINDOW_DAYS = 7

def get_context(context):
	ensure_logged_in()

	context.body_class = "forms-portal-dark"
	context.nav_active = "dashboard"
	context.title = _("Forms — My Dashboard")
	context.full_name = frappe.utils.get_fullname(frappe.session.user)

	# The Activity panel (logins, who made an invoice/payment/payroll, who
	# deleted anything, who's online, live numbers) surfaces site-wide/
	# cross-user data no restricted portal user should see just because
	# "dashboard" is a UNIVERSAL_PAGES page everyone can open (see
	# permissions.py) -- gated here to the same admin tier every sensitive
	# report page in this app already uses. The whitelisted endpoints it
	# calls (get_activity() below) enforce this for real via ensure_admin();
	# this flag only decides whether the section's markup renders at all.
	context.show_activity_section = is_admin_user()

	# Daily-work shortcuts + one number card, shown only on the ERPNext-backed
	# site (this Dashboard is shared by every site the forms app runs on —
	# your-site.example.com has no Company at all, same GL_LINKS_SITES gate every
	# other ERPNext-dependent feature in this app already uses). Reuses the
	# already-built, already-tested get_undeposited_payment_entries() rather
	# than writing a new financial aggregate query here.
	#
	# These numbers are the whole organization's, not "the selected company"
	# the way every *other* page in this app scopes itself: get_dashboard_
	# companies() returns every company the user may see unless the request
	# explicitly asked for one via the /c/<abbr>/ URL prefix (see that
	# function's own docstring, and the report that prompted this: a bare
	# /forms/user/dashboard hit was showing just one company's numbers,
	# whichever happened to be the user's own personal default).
	context.show_gl_shortcuts = frappe.local.site in GL_LINKS_SITES
	context.undeposited_summary = None
	context.outstanding_sales_summary = None
	context.sales_activity = None
	context.dashboard_scope_label = None
	if context.show_gl_shortcuts:
		companies = get_dashboard_companies()
		if len(companies) == 1:
			context.dashboard_scope_label = companies[0]
		elif companies:
			context.dashboard_scope_label = _("All Companies")

		if companies:
			from ez_accounting.ez_accounting.doctype.forms_bank_deposit_sheet.forms_bank_deposit_sheet import (
				get_undeposited_payment_entries,
			)

			# get_undeposited_payment_entries() takes one company at a time
			# (every other call site wants exactly that) -- summed here in
			# Python across companies rather than changing its contract.
			undeposited_count = 0
			undeposited_total = 0.0
			for c in companies:
				entries = get_undeposited_payment_entries(c)
				undeposited_count += len(entries)
				undeposited_total += sum(flt(e["paid_amount"]) for e in entries)
			context.undeposited_summary = {"count": undeposited_count, "total": undeposited_total}

			# Both new number cards below are plain frappe.get_all() reads,
			# summed in Python — same "no new SQL aggregate" discipline as
			# undeposited_summary above, just against Sales Invoice instead of
			# Payment Entry.
			outstanding_rows = frappe.get_all(
				"Sales Invoice",
				filters={"company": ["in", companies], "docstatus": 1, "outstanding_amount": [">", 0]},
				fields=["outstanding_amount"],
				ignore_permissions=True,
			)
			context.outstanding_sales_summary = {
				"count": len(outstanding_rows),
				"total": sum(flt(r.outstanding_amount) for r in outstanding_rows),
			}

			def _sales_period_stat(posting_date_filter):
				rows = frappe.get_all(
					"Sales Invoice",
					filters={"company": ["in", companies], "docstatus": 1, "posting_date": posting_date_filter},
					fields=["grand_total"],
					ignore_permissions=True,
				)
				return {"count": len(rows), "total": sum(flt(r.grand_total) for r in rows)}

			today = nowdate()
			month_start = get_first_day(today)
			last_month_start = get_first_day(today, d_months=-1)
			context.sales_activity = {
				# "Today" is an exact match, not >= today — this app allows a
				# backdated (or, less commonly, forward-dated) Posting Date, so
				# ">= today" isn't the same thing as "today" specifically.
				"today": _sales_period_stat(today),
				"week": _sales_period_stat([">=", get_first_day_of_week(today)]),
				"month": _sales_period_stat([">=", month_start]),
				# Full previous calendar month — bounded on both ends, unlike
				# the running "this month" figure above.
				"last_month": _sales_period_stat(
					["between", [last_month_start, add_days(month_start, -1)]]
				),
			}

	return context


def _bulk_full_names(users: list[str]) -> dict[str, str]:
	"""One query for every user name a batch of activity/deletion/session
	rows needs, instead of get_fullname() called once per row."""
	users = sorted({u for u in users if u})
	if not users:
		return {}
	rows = frappe.get_all("User", filters={"name": ["in", users]}, fields=["name", "full_name"], ignore_permissions=True)
	names = {r.name: r.full_name or r.name for r in rows}
	# A user that's since been deleted/renamed still shows up in old
	# Activity Log / Deleted Document rows -- fall back to the raw id.
	for u in users:
		names.setdefault(u, u)
	return names


def _online_rows():
	"""Every user who's real-time "online" right now -- shared by
	get_online_count() (any logged-in user, count only) and get_activity()
	(admin-only, full rows with names) so the two can never disagree about
	who counts as "online". Backed by ez_accounting.api.get_online_users()'s
	ping()-based Redis tracking, NOT tabSessions -- see that module's own
	"Real-time who's online" section for why a Sessions-table query can lag
	real online/offline transitions by up to ~10 minutes."""
	return [
		frappe._dict({"user": r["user"], "last_seen": r["last_seen"]})
		for r in get_online_users()
	]


@frappe.whitelist()
def get_online_count():
	"""Just the number of users online -- deliberately open to any logged-in
	user (ensure_logged_in(), not ensure_admin()), unlike get_activity()'s
	full Activity panel below: a bare count carries none of the identity/
	deletion detail that panel is gated for, so it's shown right at the top
	of the Dashboard hero for everyone, not just admin-tier users.
	"""
	ensure_logged_in()
	return {"online_count": len(_online_rows())}


@frappe.whitelist()
def get_activity():
	"""Everything the Dashboard's Activity panel needs in one round trip:
	live summary numbers (for the animated stat tiles), a merged recent-
	activity feed (logins, invoices, payments, payroll, deletions), and
	who's online right now. ensure_admin() is the real gate -- get_context()'s
	own show_activity_section flag only decides whether the section's
	markup is on the page at all; this endpoint enforces it independently,
	the same "the page hides it, the endpoint refuses it" pattern every
	other admin-only action in this app already follows.
	"""
	ensure_admin()

	# Whole organization by default (every company the user may see), not
	# "the selected company" -- narrowed to one only when the request
	# explicitly asked for it via the /c/<abbr>/ URL prefix. See
	# get_dashboard_companies()'s own docstring.
	companies = get_dashboard_companies() if frappe.local.site in GL_LINKS_SITES else []
	today_start = frappe.utils.get_datetime(nowdate())
	week_start = frappe.utils.get_datetime(get_first_day_of_week(nowdate()))
	feed_since = add_to_date(frappe.utils.now_datetime(), days=-ACTIVITY_FEED_WINDOW_DAYS)

	# --- online users --------------------------------------------------
	online_rows = _online_rows()

	# --- logins ----------------------------------------------------------
	login_rows = frappe.get_all(
		"Activity Log",
		filters={"operation": "Login", "status": "Success", "creation": [">=", feed_since]},
		fields=["user", "full_name", "creation"],
		order_by="creation desc",
		limit_page_length=15,
		ignore_permissions=True,
	)
	logins_today = frappe.db.count(
		"Activity Log",
		filters={"operation": "Login", "status": "Success", "creation": [">=", today_start]},
	)

	# --- deletions ---------------------------------------------------------
	deletion_rows = frappe.get_all(
		"Deleted Document",
		filters={"deleted_doctype": ["in", ACTIVITY_DELETION_DOCTYPES], "creation": [">=", feed_since]},
		fields=["owner", "deleted_doctype", "deleted_name", "creation"],
		order_by="creation desc",
		limit_page_length=15,
		ignore_permissions=True,
	)
	deletions_today = frappe.db.count(
		"Deleted Document",
		filters={"deleted_doctype": ["in", ACTIVITY_DELETION_DOCTYPES], "creation": [">=", today_start]},
	)

	invoice_rows, payment_rows, payroll_rows = [], [], []
	invoices_today = payments_today = payroll_week = 0
	invoices_today_total = payments_today_total = 0.0

	if companies:
		invoice_rows = frappe.get_all(
			"Sales Invoice",
			filters={"company": ["in", companies], "docstatus": 1, "creation": [">=", feed_since]},
			fields=["name", "owner", "creation", "grand_total"],
			order_by="creation desc",
			limit_page_length=15,
			ignore_permissions=True,
		)
		payment_rows = frappe.get_all(
			"Payment Entry",
			filters={"company": ["in", companies], "docstatus": 1, "creation": [">=", feed_since]},
			fields=["name", "owner", "creation", "paid_amount", "payment_type"],
			order_by="creation desc",
			limit_page_length=15,
			ignore_permissions=True,
		)
		payroll_rows = frappe.get_all(
			"Payroll Entry",
			filters={"company": ["in", companies], "docstatus": 1, "creation": [">=", feed_since]},
			fields=["name", "owner", "creation", "posting_date"],
			order_by="creation desc",
			limit_page_length=15,
			ignore_permissions=True,
		)

		today_invoices = frappe.get_all(
			"Sales Invoice",
			filters={"company": ["in", companies], "docstatus": 1, "creation": [">=", today_start]},
			fields=["grand_total"],
			ignore_permissions=True,
		)
		invoices_today = len(today_invoices)
		invoices_today_total = sum(flt(r.grand_total) for r in today_invoices)

		today_payments = frappe.get_all(
			"Payment Entry",
			filters={"company": ["in", companies], "docstatus": 1, "creation": [">=", today_start]},
			fields=["paid_amount"],
			ignore_permissions=True,
		)
		payments_today = len(today_payments)
		payments_today_total = sum(flt(r.paid_amount) for r in today_payments)

		payroll_week = frappe.db.count(
			"Payroll Entry",
			filters={"company": ["in", companies], "docstatus": 1, "creation": [">=", week_start]},
		)

	# --- merge everything into one feed, newest first -------------------
	all_users = (
		[r.user for r in online_rows]
		+ [r.user for r in login_rows]
		+ [r.owner for r in deletion_rows]
		+ [r.owner for r in invoice_rows]
		+ [r.owner for r in payment_rows]
		+ [r.owner for r in payroll_rows]
	)
	full_names = _bulk_full_names(all_users)

	feed = []
	for r in login_rows:
		feed.append(
			{
				"kind": "login",
				"user": r.user,
				"full_name": r.full_name or full_names.get(r.user, r.user),
				"label": _("logged in"),
				"detail": "",
				"creation": r.creation,
			}
		)
	for r in deletion_rows:
		feed.append(
			{
				"kind": "delete",
				"user": r.owner,
				"full_name": full_names.get(r.owner, r.owner),
				"label": _("deleted {0}").format(r.deleted_doctype),
				"detail": r.deleted_name,
				"creation": r.creation,
			}
		)
	for r in invoice_rows:
		feed.append(
			{
				"kind": "invoice",
				"user": r.owner,
				"full_name": full_names.get(r.owner, r.owner),
				"label": _("created invoice"),
				"detail": "{0} — {1}".format(r.name, frappe.utils.fmt_money(r.grand_total)),
				"url": "/forms/user/sales_edit?id=" + quote(r.name),
				"creation": r.creation,
			}
		)
	for r in payment_rows:
		verb = _("recorded a payment") if r.payment_type == "Receive" else _("made a payment")
		feed.append(
			{
				"kind": "payment",
				"user": r.owner,
				"full_name": full_names.get(r.owner, r.owner),
				"label": verb,
				"detail": "{0} — {1}".format(r.name, frappe.utils.fmt_money(r.paid_amount)),
				"url": "/forms/user/printer?id=" + quote(r.name),
				"creation": r.creation,
			}
		)
	for r in payroll_rows:
		feed.append(
			{
				"kind": "payroll",
				"user": r.owner,
				"full_name": full_names.get(r.owner, r.owner),
				"label": _("ran payroll"),
				"detail": "{0} — {1}".format(r.name, frappe.utils.format_date(r.posting_date)),
				"url": "/forms/user/printer?id=" + quote(r.name),
				"creation": r.creation,
			}
		)

	feed.sort(key=lambda row: row["creation"], reverse=True)
	feed = feed[:30]
	for row in feed:
		row["creation"] = frappe.utils.get_datetime_str(row["creation"])

	online = [
		{"user": r.user, "full_name": full_names.get(r.user, r.user), "last_seen": frappe.utils.get_datetime_str(r.last_seen)}
		for r in online_rows
	]

	return {
		"summary": {
			"online_count": len(online_rows),
			"logins_today": logins_today,
			"deletions_today": deletions_today,
			"invoices_today": invoices_today,
			"invoices_today_total": invoices_today_total,
			"payments_today": payments_today,
			"payments_today_total": payments_today_total,
			"payroll_week": payroll_week,
			# All companies on this site share one currency in practice, so
			# the first is good enough for formatting the money tiles above --
			# not worth a per-row currency breakdown for a dashboard summary.
			"currency": frappe.get_cached_value("Company", companies[0], "default_currency") if companies else None,
		},
		"feed": feed,
		"online": online,
		"has_company_data": bool(companies),
	}
