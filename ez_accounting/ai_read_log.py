"""Shared usage/status logger for the "Forms AI Read Log" doctype.

Used by both ez_accounting.www.forms.user.files / ez_accounting.www.forms.admin.finder ("AI
Read" and "AI read and obtain to matched company") and forms.api (the "mini
Martin" chat button). Logs are displayed on /forms/admin/ailogs. This is kept
as its own leaf module, with no imports from
either of those, specifically so neither one needs to import from the other
(files.py already imports from ez_accounting.api for its permission/company
helpers, so forms.api importing back from files.py would be circular).
"""

import json

import frappe


def log_ai_read(
	reference_doctype, reference_name, action, model, status, payload=None,
	note="", duration_ms=None, output_text=None, input_text=None,
):
	"""Best-effort usage/status log for one local-`claude`-CLI call, shown on
	the admin AI Logs page. Never allowed to break the
	calling flow -- a logging failure is swallowed (and sent to the Error
	Log) rather than surfacing as a user-facing error on top of whatever
	already happened.
	"""
	if not (reference_doctype and reference_name and action):
		return
	try:
		payload = payload or {}
		usage = (payload or {}).get("usage") or {}
		if output_text is None:
			output_text = payload.get("result")
			if output_text is None:
				output_text = payload.get("structured_output")
		if output_text is not None and not isinstance(output_text, str):
			output_text = json.dumps(output_text, ensure_ascii=False, indent=2, default=str)
		frappe.get_doc(
			{
				"doctype": "Forms AI Read Log",
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"action": action,
				"model": model,
				"status": status,
				"cost_usd": (payload or {}).get("total_cost_usd") or (payload or {}).get("cost_usd") or 0,
				"input_tokens": usage.get("input_tokens") or 0,
				"output_tokens": usage.get("output_tokens") or 0,
				"duration_ms": (payload or {}).get("duration_ms") or duration_ms or 0,
				"note": (note or "")[:500],
				"input_text": input_text or "",
				"output_text": output_text or "",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="forms.ai_read_log", message=frappe.get_traceback())


def log_ai_read_start(reference_doctype, reference_name, action, model, input_text=None):
	"""Insert a "Running" row immediately, before the actual CLI call starts
	(used by mini Martin's background/queued chat task). A task that dies
	without ever reaching completion -- worker killed, OOM, a code-editing
	task restarting its own worker process via live_reload -- then still
	leaves a visible stuck "Running" row on the admin AI Logs page instead of
	no trace at all. Returns the new row's name (pass to log_ai_read_update()
	when the call finishes), or "" if the insert itself failed -- never
	allowed to block the caller.
	"""
	if not (reference_doctype and reference_name and action):
		return ""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Forms AI Read Log",
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"action": action,
				"model": model,
				"status": "Running",
				"input_text": input_text or "",
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		frappe.log_error(title="forms.ai_read_log", message=frappe.get_traceback())
		return ""


def log_ai_read_update(name, status, payload=None, note="", duration_ms=None, output_text=None, input_text=None):
	"""Update the row log_ai_read_start() created, once the call actually
	finishes -- success or error. A no-op if `name` is blank (the start-log
	insert itself failed) rather than creating a second, unstarted row.
	"""
	if not name:
		return
	try:
		payload = payload or {}
		usage = payload.get("usage") or {}
		if output_text is None:
			output_text = payload.get("result")
			if output_text is None:
				output_text = payload.get("structured_output")
		if output_text is not None and not isinstance(output_text, str):
			output_text = json.dumps(output_text, ensure_ascii=False, indent=2, default=str)
		values = {
			"status": status,
			"cost_usd": payload.get("total_cost_usd") or payload.get("cost_usd") or 0,
			"input_tokens": usage.get("input_tokens") or 0,
			"output_tokens": usage.get("output_tokens") or 0,
			"duration_ms": payload.get("duration_ms") or duration_ms or 0,
			"note": (note or "")[:500],
			"output_text": output_text or "",
		}
		if input_text is not None:
			values["input_text"] = input_text
		frappe.db.set_value(
			"Forms AI Read Log",
			name,
			values,
		)
		frappe.db.commit()
	except Exception:
		frappe.log_error(title="forms.ai_read_log", message=frappe.get_traceback())
