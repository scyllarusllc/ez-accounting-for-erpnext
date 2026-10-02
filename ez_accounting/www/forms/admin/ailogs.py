"""Administration page for AI call usage, status, and generated output."""

import frappe
from frappe import _

from ez_accounting.permissions import ensure_explicit_admin_page_access

no_cache = 1


def get_context(context):
	ensure_explicit_admin_page_access("admin_ailogs")
	context.body_class = "forms-portal-dark"
	context.nav_active = "ailogs"
	context.title = _("AI Logs")
	return context


@frappe.whitelist()
def get_ai_read_logs(limit=50, start=0, status=None, search=None):
	"""Return one page of AI calls (without the heavy text columns) plus totals."""
	ensure_explicit_admin_page_access("admin_ailogs")
	limit = min(frappe.utils.cint(limit) or 50, 200)
	start = max(frappe.utils.cint(start), 0)

	filters = {}
	if status in ("Success", "Error", "Running"):
		filters["status"] = status
	or_filters = None
	if search:
		like = f"%{search}%"
		or_filters = {"reference_name": ["like", like], "action": ["like", like], "note": ["like", like]}

	rows = frappe.get_all(
		"Forms AI Read Log",
		fields=[
			"name", "reference_doctype", "reference_name", "action", "model", "status",
			"cost_usd", "input_tokens", "output_tokens", "duration_ms", "note", "creation", "owner",
		],
		filters=filters,
		or_filters=or_filters,
		order_by="creation desc",
		limit_start=start,
		limit_page_length=limit + 1,
	)
	if rows:
		lens = frappe.db.sql(
			"select name, length(output_text) as n, left(output_text, 300) as p from `tabForms AI Read Log` where name in %s",
			(tuple(r.name for r in rows),), as_dict=True,
		)
		lens = {d.name: (d.n or 0, d.p or "") for d in lens}
		for r in rows:
			r["output_len"], r["output_preview"] = lens.get(r.name, (0, ""))
	docs = [r.reference_name for r in rows if r.reference_doctype == "Forms Company Document"]
	if docs:
		comp = {
			d.name: d for d in frappe.db.sql(
				"""select f.name, f.company, c.abbr as company_abbr, f.title
				from `tabForms Company Document` f left join `tabCompany` c on c.name = f.company
				where f.name in %s""", (tuple(docs),), as_dict=True,
			)
		}
		for r in rows:
			d = comp.get(r.reference_name)
			if d:
				r["company"], r["company_abbr"], r["reference_title"] = d.company, d.company_abbr, d.title
	has_more = len(rows) > limit
	rows = rows[:limit]

	summary = frappe.db.sql(
		"""
		select
			count(*) as total_calls,
			sum(case when status = 'Success' then 1 else 0 end) as success_count,
			sum(case when status = 'Error' then 1 else 0 end) as error_count,
			sum(case when status = 'Running' then 1 else 0 end) as running_count,
			coalesce(sum(cost_usd), 0) as total_cost,
			coalesce(sum(input_tokens), 0) as total_input_tokens,
			coalesce(sum(output_tokens), 0) as total_output_tokens
		from `tabForms AI Read Log`
		""",
		as_dict=True,
	)[0]

	return {"rows": rows, "summary": summary, "has_more": has_more}


@frappe.whitelist()
def get_ai_read_log_text(name):
	"""Return the saved input/output text of one log row (loaded on demand)."""
	ensure_explicit_admin_page_access("admin_ailogs")
	return frappe.db.get_value("Forms AI Read Log", name, ["input_text", "output_text"], as_dict=True) or {}
