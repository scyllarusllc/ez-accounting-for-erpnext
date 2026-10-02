"""Administration route for the shared Forms document finder."""

import copy
import os

import frappe
from frappe import _

from ez_accounting.doc_access import add_visibility
from ez_accounting.permissions import ensure_explicit_admin_page_access
from ez_accounting.www.forms.admin.aiagent import EFFORT_OPTIONS, PROVIDER_MODELS
from ez_accounting.www.forms.user.files import (
	CATEGORIES,
	_AI_READ_SCHEMA,
	_clean_ai_date,
	_run_claude_extract,
)
from ez_accounting.www.forms.user.finder import search as search_documents

no_cache = 1


def _ai_read_settings():
	doc = frappe.get_single("Forms AI Agent Settings")
	provider = doc.ai_read_provider or "Claude CLI"
	model = doc.ai_read_model or ("gpt-6-luna" if provider == "Codex CLI" else "claude-haiku-4-5")
	return {
		"provider": provider,
		"model": model,
		"effort": doc.ai_read_effort or "medium",
		"provider_models": PROVIDER_MODELS,
		"effort_options": EFFORT_OPTIONS,
	}


def get_context(context):
	ensure_explicit_admin_page_access("admin_finder")
	context.body_class = "forms-portal-dark"
	context.nav_active = "finder"
	context.title = _("Document Finder")
	context.categories = CATEGORIES
	return context


@frappe.whitelist(methods=["GET"])
def search(q="", company="", category="", document_from="", document_to="", expiry_from="", expiry_to="", page=0):
	ensure_explicit_admin_page_access("admin_finder")
	return search_documents(q, company, category, document_from, document_to, expiry_from, expiry_to, page)


@frappe.whitelist(methods=["GET"])
def get_ai_read_settings():
	ensure_explicit_admin_page_access("admin_finder")
	return _ai_read_settings()


@frappe.whitelist()
def save_ai_read_settings(provider="Claude CLI", model="", effort="medium"):
	ensure_explicit_admin_page_access("admin_finder")
	if provider not in PROVIDER_MODELS:
		frappe.throw(_("Please select a valid CLI provider."))
	if model not in PROVIDER_MODELS[provider]:
		frappe.throw(_("Please select a valid model."))
	if effort not in EFFORT_OPTIONS:
		frappe.throw(_("Please select a valid thinking depth."))

	doc = frappe.get_single("Forms AI Agent Settings")
	doc.ai_read_provider = provider
	doc.ai_read_model = model
	doc.ai_read_effort = effort
	doc.save(ignore_permissions=True)
	frappe.db.commit()
	frappe.clear_document_cache("Forms AI Agent Settings")
	return _ai_read_settings()


@frappe.whitelist()
def get_email_documents():
	ensure_explicit_admin_page_access("admin_finder")
	rows = frappe.db.sql(
		"""
		select
			ea.name, ea.email_date, ea.sender, ea.subject, ea.file_name, ea.attachment,
			ea.source_communication, f.file_size, f.content_hash
		from `tabEmail Attachments` ea
		left join `tabFile` f on f.name = ea.source_file
		order by ea.email_date desc, ea.creation desc
		limit 500
		""",
		as_dict=True,
	)

	# "Already filed" match against /forms/user/files (Forms Company Document):
	# MD5 (content_hash) first since it's an exact content match; file_size is
	# only a fallback for rows whose content_hash wasn't captured (e.g. filed
	# before that field existed, see the content_hash backfill patch). Keeps
	# name/title too, so a filed row can show and link to what it was filed as.
	filed_by_hash = {}
	filed_by_size = {}
	for d in frappe.get_all(
		"Forms Company Document", filters=add_visibility([]),
		fields=["name", "title", "content_hash", "file_size"],
	):
		if d.content_hash and d.content_hash not in filed_by_hash:
			filed_by_hash[d.content_hash] = d
		if d.file_size and d.file_size not in filed_by_size:
			filed_by_size[d.file_size] = d

	for row in rows:
		matched = None
		if row.get("content_hash"):
			matched = filed_by_hash.get(row["content_hash"])
		elif row.get("file_size"):
			matched = filed_by_size.get(row["file_size"])
		row["in_file_list"] = bool(matched)
		row["filed_document"] = matched.name if matched else ""
		row["filed_title"] = matched.title if matched else ""

	return rows


def _company_match_schema(companies):
	"""_AI_READ_SCHEMA plus a `company` field constrained to an exact name
	from the given company list (or empty string) -- constraining via enum
	rather than free text means the result is always either a real Company
	name or unmatched, no fuzzy string-matching needed on our end."""
	schema = copy.deepcopy(_AI_READ_SCHEMA)
	schema["properties"]["company"] = {
		"type": "string",
		"enum": [c.name for c in companies] + [""],
		"description": (
			"The exact company name (from the list given in the prompt) this "
			"document is addressed to or belongs to, based on its letterhead, "
			"addressee, account holder, or other content -- or an empty string "
			"if none of the listed companies clearly match."
		),
	}
	schema["required"] = [*schema["required"], "company"]
	return schema


@frappe.whitelist()
def ai_file_email_document(name):
	"""AI read and obtain to matched company: extracts the same metadata as
	/forms/user/files' own "AI Read", plus which company (from this site's
	real Company list) the document belongs to, then -- only when a company is
	matched -- files a copy into that company's Forms Company Document list
	(same as an admin uploading it there by hand), so archived email
	attachments don't have to be re-found and re-filed manually one by one.
	"""
	ensure_explicit_admin_page_access("admin_finder")
	return _ai_file_email_document(name)


def _ai_file_email_document(name):
	"""Run the email-attachment filing flow after the caller checks access."""
	ea = frappe.get_doc("Email Attachments", name)

	file_names = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Email Attachments", "attached_to_name": ea.name},
		pluck="name",
		order_by="creation desc",
		limit_page_length=1,
	)
	if not file_names:
		frappe.throw(_("No attachment file was found for this email document."))

	file_doc = frappe.get_doc("File", file_names[0])
	full_path = file_doc.get_full_path()
	if not os.path.exists(full_path):
		frappe.throw(_("The file could not be found on disk."))

	companies = frappe.get_all("Company", fields=["name", "abbr"], order_by="name asc")
	if not companies:
		frappe.throw(_("No companies are configured."))

	company_lines = "\n".join(f"- {c.name} ({c.abbr})" for c in companies)
	extra_instructions = (
		"Determine which ONE of the following companies this document belongs to, "
		"based on the addressee, letterhead, account holder, or other content in the "
		"file (match by name or abbreviation). Set the \"company\" field to that "
		"company's exact name from this list, or an empty string if none of them "
		"clearly match:\n" + company_lines
	)

	structured = _run_claude_extract(
		full_path, file_doc.file_name,
		schema=_company_match_schema(companies),
		extra_instructions=extra_instructions,
		log_reference_doctype="Email Attachments", log_reference_name=ea.name, log_action="AI File to Company",
	)

	valid_names = {c.name for c in companies}
	matched_company = structured.get("company") or ""
	if matched_company not in valid_names:
		matched_company = ""

	category = structured.get("category") or ""
	if category not in CATEGORIES:
		category = "Other"

	suggested = {
		"title": (structured.get("title") or ea.subject or ea.file_name or "").strip(),
		"category": category,
		"tags": structured.get("tags") or "",
		"document_date": _clean_ai_date(structured.get("document_date")),
		"expiry_date": _clean_ai_date(structured.get("expiry_date")),
		"description": structured.get("description") or "",
	}

	# Annotate the log entry _run_claude_extract() already wrote with the
	# match outcome -- it's decided here, after the call returns, not inside
	# _run_claude_extract() itself.
	latest_log = frappe.get_all(
		"Forms AI Read Log",
		filters={"reference_doctype": "Email Attachments", "reference_name": ea.name, "action": "AI File to Company"},
		order_by="creation desc",
		limit_page_length=1,
		pluck="name",
	)
	if latest_log:
		note = f"Matched: {matched_company}" if matched_company else "No matching company found."
		frappe.db.set_value("Forms AI Read Log", latest_log[0], "note", note, update_modified=False)
		frappe.db.commit()

	if not matched_company:
		return {"matched_company": "", "suggested": suggested, "document_name": None}

	content = structured.get("content") or ""
	doc = frappe.get_doc(
		{
			"doctype": "Forms Company Document",
			"company": matched_company,
			"title": suggested["title"] or file_doc.file_name,
			"category": suggested["category"],
			"document_date": suggested["document_date"] or None,
			"expiry_date": suggested["expiry_date"] or None,
			"description": suggested["description"],
			"tags": suggested["tags"],
		}
	)
	doc.insert(ignore_permissions=True)

	new_file = frappe.get_doc(
		{
			"doctype": "File",
			"attached_to_doctype": "Forms Company Document",
			"attached_to_name": doc.name,
			"file_name": file_doc.file_name,
			"is_private": 1,
			"content": file_doc.get_content(),
		}
	).save(ignore_permissions=True)

	now = frappe.utils.now_datetime()
	frappe.db.set_value(
		"Forms Company Document",
		doc.name,
		{
			"file": new_file.file_url,
			"file_name": new_file.file_name,
			"file_size": new_file.file_size or 0,
			"content_hash": new_file.content_hash or "",
			"content": content,
			"ai_read_at": now,
		},
	)
	frappe.db.commit()

	return {
		"matched_company": matched_company,
		"document_name": doc.name,
		"suggested": suggested,
	}
