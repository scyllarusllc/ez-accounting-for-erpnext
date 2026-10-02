# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

"""A per-company document library — compliance papers, tax forms, licenses,
insurance, anything else worth keeping on hand — for the selected company.
Built for organizing/searching/finding, not accounting: no GL postings, no
sequence numbers, just metadata (Category, Tags, Document Date, Expiry Date)
on top of a private uploaded file (see [[forms-payments-sequence]] and
sibling report pages for the app's other per-doctype conventions this one
deliberately does NOT follow, since it isn't a financial register).

Storage: one Forms Company Document record per file, the file itself a
private frappe File attached to it (attached_to_doctype/attached_to_name),
created the same way upload_sales_invoice_attachment() does (see that
docstring) rather than through the doctype's own Attach widget or Frappe's
desk-style /api/method/upload_file -- both need doctype *write* permission
this portal role wouldn't otherwise be checked against per-record.

Access: Forms Company Document's own DocType permissions grant System
Manager and the GATE_ROLE ("Ez Accounting Admin") read/write/create/delete, so
frappe.get_doc(...).check_permission(...) enforces both the gate role AND
ERPNext's normal Company User Permission scoping (see
[[forms-user-permissions]]) for free -- including on the private file's own
`file_url` link, which the browser fetches straight through Frappe's normal
private-file permission check (same as sales_edit.html's attachments list).
The on-screen listing filters through apply_permitted_filter() same as every
other report page, so a restricted user's fetch never depends on the doctype
check alone.
"""

import copy
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from xml.etree import ElementTree

import frappe
from frappe import _

from ez_accounting.ai_read_log import log_ai_read as _log_ai_read
from ez_accounting.doc_access import add_visibility, assert_can_access
from ez_accounting.api import apply_permitted_filter, ensure_admin, get_selected_company

no_cache = 1

# "AI Read" shells out to the local `claude` CLI (Claude Code) rather than
# calling a hosted API with a key this app would have to manage -- it rides
# whatever interactive login/subscription is already active for the OS user
# `frappe serve` runs as (see files.py's own docstring below, at ai_read()).
AI_READ_TIMEOUT_SECONDS = 180

# Pinned rather than left on the CLI's own default -- this is a mechanical
# extraction task (title/tags/dates/description/company match), not
# open-ended reasoning, so the cheapest current model is the right tradeoff
# (see the "which model" conversation this was added from).
AI_READ_MODEL = "claude-haiku-4-5"


def _get_ai_read_settings():
	"""Return the dedicated Finder AI Read CLI settings with safe defaults."""
	doc = frappe.get_cached_doc("Forms AI Agent Settings")
	provider = doc.ai_read_provider or "Claude CLI"
	return {
		"provider": provider,
		"model": doc.ai_read_model or ("gpt-6-luna" if provider == "Codex CLI" else AI_READ_MODEL),
		"effort": doc.ai_read_effort or "medium",
	}


def _codex_document_input(path: str, file_name: str) -> tuple[str, list[str]]:
	"""Read document content in Python so Codex needs no filesystem tools."""
	mime = (mimetypes.guess_type(file_name)[0] or "").lower()
	ext = os.path.splitext(file_name)[1].lower()
	if mime.startswith("image/") and ext != ".svg":
		return "The document is attached as an image.", [path]

	text = ""
	if ext == ".pdf":
		from pypdf import PdfReader

		text = "\n\n".join((page.extract_text() or "") for page in PdfReader(path).pages)
	elif ext == ".docx":
		with zipfile.ZipFile(path) as archive:
			root = ElementTree.fromstring(archive.read("word/document.xml"))
			text = "\n".join(node.text or "" for node in root.iter() if node.tag.endswith("}t"))
	elif ext == ".xlsx":
		from openpyxl import load_workbook

		book = load_workbook(path, read_only=True, data_only=True)
		lines = []
		for sheet in book.worksheets:
			lines.append(f"[{sheet.title}]")
			for row in sheet.iter_rows(values_only=True):
				lines.append("\t".join("" if value is None else str(value) for value in row))
		text = "\n".join(lines)
	else:
		with open(path, "rb") as source:
			raw = source.read(1_500_000)
		try:
			text = raw.decode("utf-8")
		except UnicodeDecodeError:
			text = raw.decode("latin-1") if mime.startswith("text/") else ""

	text = text.strip()
	if not text:
		frappe.throw(
			_("Codex could not extract readable text from this file. Use Claude CLI for scanned PDFs or this file type.")
		)
	return "Document content:\n---\n" + text[:500_000] + "\n---", []

# Single source of truth for the doctype's own Select options -- keep in sync
# with forms_company_document.json's `category` field. Duplicated into
# files.html's JS (category filter chips + the upload form's own <select>)
# since a website page has no server-rendered doctype meta to read this from.
CATEGORIES = [
	"Compliance",
	"Tax Form",
	"License & Permit",
	"Insurance",
	"Contract",
	"Financial Statement",
	"Policy",
	"Other",
]

# A document expiring within this many days is flagged "Expiring Soon"
# (still valid, but worth renewing) rather than plain "Valid".
EXPIRING_SOON_DAYS = 30


def _owner_only() -> bool:
	"""Private mode (finder_private page): every list/read/write/delete is
	limited to documents the logged-in user created."""
	return frappe.form_dict.get("private") in (1, "1", "true", True)


def _truthy(value) -> bool:
	return value in (1, "1", "true", "True", True)


def get_context(context):
	ensure_admin()
	context.private = _owner_only()
	context.embedded = bool(frappe.form_dict.get("embedded"))
	context.body_class = "forms-portal-dark files-embedded" if context.embedded else "forms-portal-dark"
	context.nav_active = "files"
	context.title = _("Files")
	context.company = get_selected_company()
	context.categories = CATEGORIES
	return context


def _expiry_status(expiry_date) -> str:
	if not expiry_date:
		return ""
	today = frappe.utils.getdate(frappe.utils.today())
	expiry_date = frappe.utils.getdate(expiry_date)
	if expiry_date < today:
		return "Expired"
	if (expiry_date - today).days <= EXPIRING_SOON_DAYS:
		return "Expiring Soon"
	return "Valid"


def _build_documents(company: str, category: str = "", search: str = "") -> list[dict]:
	"""Shared by the on-screen table and get_category_counts() -- one query,
	one place computing expiry_status, so the chip counts and the filtered
	table can never disagree with each other."""
	if not company:
		return []

	filters = [["company", "=", company]]
	if _owner_only():
		filters.append(["owner", "=", frappe.session.user])
	filters = apply_permitted_filter(filters, "Company", "company")
	filters = add_visibility(filters)

	rows = frappe.get_all(
		"Forms Company Document",
		filters=filters,
		fields=[
			"name",
			"title",
			"category",
			"document_date",
			"expiry_date",
			"file",
			"file_name",
			"file_size",
			"content_hash",
			"description",
			"tags",
			"uploaded_by",
			"creation",
			"ai_read_at",
			"is_private",
			"owner",
		],
		order_by="creation desc",
		limit_page_length=0,
	)

	for r in rows:
		r["expiry_status"] = _expiry_status(r.get("expiry_date"))

	search = (search or "").strip().lower()
	if search:
		rows = [
			r
			for r in rows
			if search in (r.title or "").lower()
			or search in (r.category or "").lower()
			or search in (r.tags or "").lower()
			or search in (r.description or "").lower()
			or search in (r.file_name or "").lower()
		]

	if category and category != "All":
		rows = [r for r in rows if r.category == category]

	return rows


@frappe.whitelist()
def get_documents(category: str = "", search: str = ""):
	"""Returns both the filtered rows AND per-category counts computed over
	just the company + search scope (not the category filter itself), so the
	filter chips can show "Compliance (4)" etc. that stay accurate no matter
	which chip is currently selected -- one round trip instead of two."""
	ensure_admin()
	company = get_selected_company()

	unfiltered = _build_documents(company, category="", search=search)
	counts = {c: 0 for c in CATEGORIES}
	for r in unfiltered:
		if r.category in counts:
			counts[r.category] += 1

	rows = unfiltered if (not category or category == "All") else [r for r in unfiltered if r.category == category]

	return {"documents": rows, "counts": counts, "total": len(unfiltered)}


def _get_document(name: str, perm_type: str = "write"):
	doc = frappe.get_doc("Forms Company Document", name)
	if _owner_only() and doc.owner != frappe.session.user:
		frappe.throw(_("This document belongs to another user."), frappe.PermissionError)
	assert_can_access(doc)
	doc.check_permission(perm_type)
	return doc


@frappe.whitelist()
def upload_document(
	title: str = "",
	category: str = "Other",
	document_date: str = "",
	expiry_date: str = "",
	description: str = "",
	tags: str = "",
	is_private: str = "",
):
	"""Multipart body: the metadata fields above as plain form fields, plus a
	single "file" part -- same shape as
	sales_edit.upload_sales_invoice_attachment(), see this module's own
	docstring for why a dedicated endpoint exists instead of posting straight
	to Frappe's own /api/method/upload_file.
	"""
	ensure_admin()
	company = get_selected_company()
	if not company:
		frappe.throw(_("No company is selected."))

	files = getattr(frappe.request, "files", None)
	uploaded = files.get("file") if files else None
	if not uploaded:
		frappe.throw(_("No file was uploaded."))

	content = uploaded.stream.read()
	filename = uploaded.filename or "upload"
	title = (title or "").strip() or os.path.splitext(os.path.basename(filename))[0] or filename
	category = category if category in CATEGORIES else "Other"

	from mimetypes import guess_type

	from frappe.handler import ALLOWED_MIMETYPES

	content_type = (getattr(uploaded, "mimetype", None) or guess_type(filename)[0] or "").lower()
	is_allowed = content_type in ALLOWED_MIMETYPES or (
		content_type.startswith("image/") and content_type != "image/svg+xml"
	)
	if not is_allowed:
		frappe.throw(_("You can only upload an image, a PDF, or a text/Office document."))

	doc = frappe.get_doc(
		{
			"doctype": "Forms Company Document",
			"company": company,
			"title": title,
			"category": category,
			"document_date": document_date or None,
			"expiry_date": expiry_date or None,
			"description": description or "",
			"tags": tags or "",
			# The private page files everything as owner-only by default.
			"is_private": 1 if (_owner_only() and is_private == "") or _truthy(is_private) else 0,
		}
	)
	doc.insert(ignore_permissions=True)

	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"attached_to_doctype": "Forms Company Document",
			"attached_to_name": doc.name,
			"file_name": filename,
			"is_private": 1,
			"content": content,
		}
	).save(ignore_permissions=True)

	doc.db_set("file", file_doc.file_url, update_modified=False)
	doc.db_set("file_name", file_doc.file_name, update_modified=False)
	doc.db_set("file_size", file_doc.file_size or 0, update_modified=False)
	doc.db_set("content_hash", file_doc.content_hash or "", update_modified=False)
	frappe.db.commit()

	doc.reload()
	row = doc.as_dict()
	row["expiry_status"] = _expiry_status(row.get("expiry_date"))
	return row


@frappe.whitelist()
def update_document(
	name: str,
	title: str,
	category: str,
	document_date: str = "",
	expiry_date: str = "",
	description: str = "",
	tags: str = "",
	is_private: str = "",
):
	"""Metadata-only edit -- re-organizing (renaming, re-categorizing,
	re-tagging, correcting a date) never re-uploads the file itself."""
	ensure_admin()
	doc = _get_document(name, "write")

	title = (title or "").strip()
	if not title:
		frappe.throw(_("Title is required."))
	if category not in CATEGORIES:
		frappe.throw(_("{0} is not a valid category.").format(category))

	doc.title = title
	doc.category = category
	doc.document_date = document_date or None
	doc.expiry_date = expiry_date or None
	doc.description = description or ""
	doc.tags = tags or ""
	if is_private != "":
		if doc.owner != frappe.session.user:
			frappe.throw(_("Only the owner can change whether a document is private."), frappe.PermissionError)
		doc.is_private = 1 if _truthy(is_private) else 0
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	row = doc.as_dict()
	row["expiry_status"] = _expiry_status(row.get("expiry_date"))
	return row


@frappe.whitelist()
def get_document(name: str):
	"""The one field the list view deliberately doesn't carry (Extracted
	Content can be long) -- fetched lazily, only when the Edit modal opens
	for a row that already has an ai_read_at, so a normal list load never
	pays for it."""
	doc = _get_document(name, "read")
	row = doc.as_dict()
	row["expiry_status"] = _expiry_status(row.get("expiry_date"))
	return row


# The doctype fields AI Read can suggest -- title/tags/dates/description are
# handed back for the person to review in the Edit modal (never silently
# overwritten); category is validated against CATEGORIES; content is the one
# field that IS saved immediately (see ai_read()'s own docstring for why).
_AI_READ_SCHEMA = {
	"type": "object",
	"properties": {
		"title": {"type": "string", "description": "A short, human-readable name for this document."},
		"category": {"type": "string", "enum": CATEGORIES, "description": "The closest matching category."},
		"tags": {"type": "string", "description": "A short comma-separated list of useful search keywords."},
		"document_date": {
			"type": "string",
			"description": "The document's own issue/effective/filed date, YYYY-MM-DD, or an empty string if none is stated.",
		},
		"expiry_date": {
			"type": "string",
			"description": "When it expires, is due, or must be renewed, YYYY-MM-DD, or an empty string if none is stated.",
		},
		"description": {"type": "string", "description": "One or two plain sentences summarizing what this document is."},
		"content": {"type": "string", "description": "The full text readable from the file, as plain text."},
	},
	"required": ["title", "category", "tags", "document_date", "expiry_date", "description", "content"],
}


def _run_claude_extract(
	source_path: str,
	file_name: str,
	schema: dict = None,
	extra_instructions: str = "",
	model: str = None,
	provider: str = None,
	effort: str = None,
	log_reference_doctype: str = None,
	log_reference_name: str = None,
	log_action: str = None,
) -> dict:
	"""Shells out to the local `claude` CLI, non-interactively (`claude -p`),
	the same binary and the same login/subscription session already active
	for whichever OS user `frappe serve` runs as -- this app has no API key
	of its own and doesn't manage one.

	The target file is first copied into its own throwaway directory, and
	*that* directory -- never the site's shared private/files/ folder, which
	holds every other user's uploads too -- is the only path handed to
	Claude, via `--add-dir`. Tool access is locked to `Read` alone
	(`--allowedTools Read`, so it can open exactly that one file and nothing
	else -- no Bash, no Glob/LS to enumerate a directory, nothing that could
	reach outside the throwaway copy) and `--permission-prompts none` (since
	nothing it could ask permission for is allowed anyway, this just keeps a
	misconfiguration from hanging the request instead of failing loudly).
	`--json-schema` gets a real Claude Code SDK feature -- structured,
	schema-validated output -- rather than asking it to describe a JSON
	shape in prose and hoping the reply parses. `--model` pins this to
	AI_READ_MODEL (Haiku) by default -- this is mechanical extraction, not
	open-ended reasoning, so the cheapest current model is the right
	tradeoff, rather than riding whatever the CLI's own default happens to
	be.

	This has a real dollar cost against that subscription and takes
	something like 10-30 seconds per call, so it's only ever invoked by an
	explicit "AI Read" click -- never automatically on upload.

	`schema`/`extra_instructions` let a caller outside this module (e.g. the
	admin Finder's "AI read and obtain to matched company" button) reuse the
	same sandboxed subprocess mechanics with a different JSON schema/prompt
	addendum -- default is this module's own metadata-only schema/prompt.

	`log_reference_doctype`/`log_reference_name`/`log_action` identify the
	calling context (e.g. "Forms Company Document" + "AI Read", or "Email
	Attachments" + "AI File to Company") so every call -- success or failure
	-- is recorded as a Forms AI Read Log for the admin AI Logs page.
	Omit them to skip logging (there is no meaningful caller-identifying
	record to attach one to).
	"""
	work_dir = tempfile.mkdtemp(prefix="forms-ai-read-")
	settings = _get_ai_read_settings()
	provider = provider or settings["provider"]
	model = model or settings["model"]
	effort = effort or settings["effort"]
	started = time.monotonic()

	def log(status, payload=None, note="", output_text=None):
		_log_ai_read(
			log_reference_doctype, log_reference_name, log_action, model, status,
			payload=payload, note=note, output_text=output_text,
			duration_ms=int((time.monotonic() - started) * 1000),
		)

	try:
		safe_name = os.path.basename(file_name or os.path.basename(source_path)) or "document"
		dest_path = os.path.join(work_dir, safe_name)
		shutil.copyfile(source_path, dest_path)

		codex_images = []
		if provider == "Codex CLI":
			document_input, codex_images = _codex_document_input(dest_path, safe_name)
			prompt = "Extract the fields per the JSON schema from the document below. Do not use tools.\n\n" + document_input
		else:
			prompt = (
				"Read the file at {0} and extract the fields per the JSON schema you were given."
			).format(dest_path)
		if extra_instructions:
			prompt += "\n\n" + extra_instructions

		output_path = None
		if provider == "Codex CLI":
			codex_bin = shutil.which("codex") or "codex"
			schema_path = os.path.join(work_dir, "output-schema.json")
			output_path = os.path.join(work_dir, "output.json")
			with open(schema_path, "w", encoding="utf-8") as schema_file:
				json.dump(schema or _AI_READ_SCHEMA, schema_file)
			cmd = [
				codex_bin, "exec", "--ephemeral", "--ignore-rules", "--skip-git-repo-check",
				"-C", work_dir, "-s", "read-only", "--model", model,
				"-c", f'model_reasoning_effort="{effort}"',
				"--output-schema", schema_path, "--output-last-message", output_path,
			]
			for image_path in codex_images:
				cmd.extend(["--image", image_path])
			cmd.append(prompt)
		else:
			claude_bin = shutil.which("claude") or "claude"
			cmd = [
				claude_bin, "-p", prompt, "--output-format", "json",
				"--json-schema", json.dumps(schema or _AI_READ_SCHEMA),
				"--model", model, "--effort", effort, "--allowedTools", "Read",
				"--add-dir", work_dir, "--permission-prompts", "none",
			]

		try:
			proc = subprocess.run(cmd, capture_output=True, text=True, timeout=AI_READ_TIMEOUT_SECONDS)
		except FileNotFoundError:
			cli_name = "codex" if provider == "Codex CLI" else "claude"
			log("Error", note=f"The local `{cli_name}` CLI isn't available on this server.")
			frappe.throw(_("The local `{0}` CLI isn't available on this server.").format(cli_name))
		except subprocess.TimeoutExpired:
			log("Error", note=f"Timed out after {AI_READ_TIMEOUT_SECONDS} seconds.")
			frappe.throw(
				_("AI Read timed out after {0} seconds. Try again, or the file may be too large/complex.").format(
					AI_READ_TIMEOUT_SECONDS
				)
			)

		if proc.returncode != 0:
			frappe.log_error(title="forms.files.ai_read", message=(proc.stderr or "")[:5000])
			log("Error", note=(proc.stderr or "").strip()[:500])
			frappe.throw(_("AI Read failed: {0}").format((proc.stderr or "").strip()[:300] or _("unknown error")))

		try:
			if provider == "Codex CLI":
				with open(output_path, encoding="utf-8") as output_file:
					structured = json.load(output_file)
				payload = {"provider": provider, "model": model, "result": structured}
			else:
				payload = json.loads(proc.stdout)
		except (OSError, ValueError):
			frappe.log_error(title="forms.files.ai_read", message=proc.stdout[:5000])
			log("Error", note="AI Read returned an unexpected response.")
			frappe.throw(_("AI Read returned an unexpected response."))

		structured = payload.get("result") if provider == "Codex CLI" else payload.get("structured_output")
		if not structured and payload.get("result"):
			try:
				structured = json.loads(payload["result"])
			except ValueError:
				structured = None
		if not structured:
			frappe.log_error(title="forms.files.ai_read", message=json.dumps(payload)[:5000])
			log("Error", payload=payload, note="AI Read did not return usable data for this file.")
			frappe.throw(_("AI Read did not return usable data for this file."))

		log("Success", payload=payload, output_text=structured)
		return structured
	finally:
		shutil.rmtree(work_dir, ignore_errors=True)


def _clean_ai_date(value) -> str:
	if not value:
		return ""
	try:
		return str(frappe.utils.getdate(value))
	except Exception:
		return ""


@frappe.whitelist()
def ai_read(name: str):
	""""AI Read" button: extracts structured metadata + the full text of a
	document's already-uploaded file via the local Claude CLI (see
	_run_claude_extract()'s own docstring for the mechanics/sandboxing).

	The extracted metadata and content are written back to the document in one
	update. The Files page keeps its modal open throughout, so the user sees
	the reading/updating state and the completed values.
	"""
	ensure_admin()
	doc = _get_document(name, "write")
	if not doc.file:
		frappe.throw(_("Upload a file first."))

	file_names = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Forms Company Document", "attached_to_name": doc.name},
		pluck="name",
		order_by="creation desc",
		limit_page_length=1,
	)
	if not file_names:
		frappe.throw(_("No file is attached to this document."))

	file_doc = frappe.get_doc("File", file_names[0])
	full_path = file_doc.get_full_path()
	if not os.path.exists(full_path):
		frappe.throw(_("The file could not be found on disk."))

	companies = frappe.get_all("Company", fields=["name", "abbr"], order_by="name asc")
	company_names = [c.name for c in companies]
	company_lines = "\n".join(f"- {c.name} ({c.abbr})" for c in companies)
	schema = copy.deepcopy(_AI_READ_SCHEMA)
	schema["properties"]["company"] = {
		"type": "string",
		"enum": company_names + [""],
		"description": "The exact company name from the prompt's list this document belongs to, or an empty string.",
	}
	schema["required"] = [*schema["required"], "company"]
	extra_instructions = (
		"Also decide which ONE of these companies the document belongs to, and set \"company\" to its "
		"exact name (or an empty string if unclear). Judge by letterhead, addressee, and above all the "
		"subject matter: degree-granting, graduate or postgraduate programs (e.g. MBA, MAT, master's or "
		"doctoral courses and their course sequences) belong to the company that is a university; "
		"K-12 or school-level material belongs to the company that is a school; match medical/clinic "
		"material to the medical company. Companies:\n" + company_lines
	)
	structured = _run_claude_extract(
		full_path, file_doc.file_name, schema=schema, extra_instructions=extra_instructions,
		log_reference_doctype="Forms Company Document", log_reference_name=doc.name, log_action="AI Read",
	)
	selected_company = structured.get("company") or ""
	if selected_company not in company_names:
		selected_company = ""
	previous_company = doc.company
	company_changed = bool(selected_company and selected_company != previous_company)
	if company_changed:
		latest_log = frappe.get_all(
			"Forms AI Read Log",
			filters={"reference_doctype": "Forms Company Document", "reference_name": doc.name, "action": "AI Read"},
			order_by="creation desc", limit_page_length=1, pluck="name",
		)
		if latest_log:
			frappe.db.set_value(
				"Forms AI Read Log", latest_log[0], "note",
				f"Company changed by AI: {previous_company} -> {selected_company}", update_modified=False,
			)

	content = structured.get("content") or ""
	now = frappe.utils.now_datetime()
	category = structured.get("category") or ""
	if category not in CATEGORIES:
		category = "Other"

	suggested = {
		"title": (structured.get("title") or doc.title or "").strip(),
		"category": category,
		"tags": structured.get("tags") or "",
		"document_date": _clean_ai_date(structured.get("document_date")),
		"expiry_date": _clean_ai_date(structured.get("expiry_date")),
		"description": structured.get("description") or "",
	}
	# MariaDB DATE columns reject an empty string in strict mode. Claude uses
	# an empty string to mean "not stated", so persist those values as SQL
	# NULL while keeping the API response convenient for date inputs.
	database_values = {
		**suggested,
		"document_date": suggested["document_date"] or None,
		"expiry_date": suggested["expiry_date"] or None,
		"content": content,
		"ai_read_at": now,
	}
	if company_changed:
		database_values["company"] = selected_company
	frappe.db.set_value(
		"Forms Company Document",
		doc.name,
		database_values,
	)
	frappe.db.commit()

	return {
		"name": doc.name,
		"content": content,
		"ai_read_at": frappe.utils.format_datetime(now, "M/d/yyyy h:mm a"),
		"suggested": suggested,
		"company_changed": company_changed,
		"previous_company": previous_company if company_changed else "",
		"company": selected_company if company_changed else previous_company,
	}


@frappe.whitelist()
def delete_document(name: str):
	doc = _get_document(name, "delete")

	for file_name in frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Forms Company Document", "attached_to_name": doc.name},
		pluck="name",
	):
		frappe.delete_doc("File", file_name, ignore_permissions=True)

	frappe.delete_doc("Forms Company Document", doc.name, ignore_permissions=True)
	frappe.db.commit()
	return {"removed": doc.name}
