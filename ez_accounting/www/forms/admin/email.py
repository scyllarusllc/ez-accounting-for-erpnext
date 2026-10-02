# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

from email.utils import parseaddr
from email import policy
from email.header import decode_header, make_header
from email.parser import Parser
import re

import frappe
from frappe import _
from frappe.utils.file_manager import get_file, save_file

from ez_accounting.permissions import ensure_explicit_admin_page_access

no_cache = 1
DEFAULT_ARCHIVE_SENDER = "scanner@example.com"
EMAIL_AI_READ_LOCK = "forms:email_ai_read"
EMAIL_AI_READ_LOCK_TIMEOUT = 2400


def get_context(context):
	ensure_explicit_admin_page_access("admin_email")
	context.body_class = "forms-portal-dark"
	context.nav_active = "email"
	context.title = _("Email")
	context.xerox_sender = DEFAULT_ARCHIVE_SENDER
	return context


def _archive_settings() -> frappe._dict:
	settings = frappe.get_single("Email Attachment Settings")
	return frappe._dict(
		email_account=settings.email_account or "",
		sender_email=(settings.sender_email or DEFAULT_ARCHIVE_SENDER).strip().lower(),
		include_spam=bool(settings.include_spam),
	)


def _sender_address(value: str) -> str:
	return (parseaddr(value or "")[1] or value or "").strip().lower()


def _copy_attachment(communication, source_file) -> str:
	archive = frappe.get_doc(
		{
			"doctype": "Email Attachments",
			"source_communication": communication.name,
			"source_file": source_file.name,
			"email_date": communication.communication_date,
			"sender": communication.sender,
			"recipients": communication.recipients,
			"subject": communication.subject,
			"file_name": source_file.file_name,
			"email_account": communication.email_account,
		}
	).insert(ignore_permissions=True)

	file_name, content = get_file(source_file.file_url)
	archived_file = save_file(file_name, content, archive.doctype, archive.name, is_private=1)
	archive.db_set("attachment", archived_file.file_url, update_modified=False)
	return archive.name


def archive_xerox_communication(communication, method=None) -> dict:
	settings = _archive_settings()
	if (
		communication.communication_medium != "Email"
		or communication.sent_or_received != "Received"
		or _sender_address(communication.sender) != settings.sender_email
		or (settings.email_account and communication.email_account != settings.email_account)
	):
		return {"created": 0, "skipped": 0}
	created = 0
	skipped = 0
	files = frappe.get_all(
		"File",
		filters={"attached_to_doctype": "Communication", "attached_to_name": communication.name, "is_folder": 0},
		fields=["name", "file_name", "file_url"],
	)
	for source_file in files:
		if frappe.db.exists("Email Attachments", {"source_file": source_file.name}):
			skipped += 1
			continue
		_copy_attachment(communication, source_file)
		created += 1
	return {"created": created, "skipped": skipped}


def archive_xerox_attachments() -> dict:
	settings = _archive_settings()
	communications = frappe.get_all(
		"Communication",
		filters={
			"communication_medium": "Email",
			"sent_or_received": "Received",
		},
		or_filters={"sender": ["like", f"%{settings.sender_email}%"]},
		fields=["name", "sender", "recipients", "subject", "communication_date", "email_account"],
		order_by="communication_date asc",
	)
	created = 0
	skipped = 0
	for communication in communications:
		if _sender_address(communication.sender) != settings.sender_email:
			continue
		if settings.email_account and communication.email_account != settings.email_account:
			continue
		result = archive_xerox_communication(frappe._dict(communication))
		created += result["created"]
		skipped += result["skipped"]
	return {"created": created, "skipped": skipped, "messages": len(communications)}


def _spam_folders(server) -> list[str]:
	status, responses = server.imap.list()
	if status != "OK":
		return []
	folders = []
	for response in responses or []:
		line = frappe.safe_decode(response)
		if "spam" not in line.lower() and "junk" not in line.lower():
			continue
		match = re.search(r'"([^"]+)"\s*$', line)
		folder = match.group(1) if match else line.rsplit(" ", 1)[-1].strip('"')
		if folder and folder not in folders:
			folders.append(folder)
	return folders


def _pull_xerox_messages(account, sender_email: str, include_spam: bool = False) -> int:
	"""Import matching IMAP messages even when the mailbox marks them read."""
	if not account.use_imap:
		return 0
	from frappe.email.receive import InboundMail

	server = account.get_incoming_server(
		in_receive=True,
		email_sync_rule=f'(FROM "{sender_email}")',
	)
	server.latest_messages = []
	server.seen_status = {}
	server.uid_reindexed = False
	processed = 0
	try:
		folders = [frappe._dict(folder_name=row.folder_name, append_to=row.append_to, uidvalidity=row.uidvalidity) for row in account.imap_folder]
		if not folders:
			folders = [frappe._dict(folder_name="INBOX", append_to=None, uidvalidity=None)]
		if include_spam:
			known = {row.folder_name for row in folders}
			folders.extend(
				frappe._dict(folder_name=name, append_to=None, uidvalidity=None)
				for name in _spam_folders(server) if name not in known
			)
		for folder in folders:
			folder_name = folder.folder_name or "INBOX"
			if not server.select_imap_folder(folder_name):
				continue
			server.settings["uid_validity"] = getattr(folder, "uidvalidity", None)
			for index, uid in enumerate(reversed(server.get_new_mails(f'"{folder_name}"'))):
				before = len(server.latest_messages)
				server.retrieve_message(uid, index + 1, f'"{folder_name}"')
				if len(server.latest_messages) == before:
					continue
				mail = InboundMail(
					server.latest_messages[-1],
					account,
					frappe.safe_decode(uid),
					server.seen_status.get(uid),
					folder.append_to,
				)
				if _sender_address(mail.from_email) != sender_email:
					continue
				mail.process()
				frappe.db.commit()
				processed += 1
	finally:
		server.logout()
	return processed


@frappe.whitelist()
def fetch_inbox():
	ensure_explicit_admin_page_access("admin_email")
	return _fetch_inbox()


def _fetch_inbox():
	"""Synchronize and archive configured email without a web-session check."""
	from frappe.email.doctype.email_account.email_account import pull_from_email_account

	settings = _archive_settings()
	account_filters = {"enable_incoming": 1, "awaiting_password": 0}
	if settings.email_account:
		account_filters["name"] = settings.email_account
	accounts = frappe.get_all(
		"Email Account",
		filters=account_filters,
		pluck="name",
	)
	xerox_fetched = 0
	for account_name in accounts:
		pull_from_email_account(account_name)
		xerox_fetched += _pull_xerox_messages(
			frappe.get_doc("Email Account", account_name), settings.sender_email, settings.include_spam
		)
	result = archive_xerox_attachments()
	result["accounts"] = len(accounts)
	result["xerox_fetched"] = xerox_fetched
	return result


def _next_ai_read_attachment():
	"""Return the newest attachment not already successfully AI-read.

	A successful extraction counts as processed even when no company matched.
	Errors remain eligible so a transient CLI failure can be retried.
	"""
	rows = frappe.db.sql(
		"""
		select ea.name, ea.file_name, ea.subject
		from `tabEmail Attachments` ea
		where not exists (
			select 1
			from `tabForms AI Read Log` log
			where log.reference_doctype = 'Email Attachments'
				and log.reference_name = ea.name
				and log.action = 'AI File to Company'
				and log.status = 'Success'
		)
		order by ea.email_date desc, ea.creation desc
		limit 1
		""",
		as_dict=True,
	)
	return rows[0] if rows else None


@frappe.whitelist()
def fetch_one_email_and_ai_read():
	"""Sync mail, then AI-read exactly one not-yet-processed attachment."""
	ensure_explicit_admin_page_access("admin_email")
	return _fetch_one_email_and_ai_read_locked()


def _fetch_one_email_and_ai_read_locked():
	"""Run one fetch/read cycle, without allowing overlapping workers."""
	lock = frappe.cache.lock(
		frappe.cache.make_key(EMAIL_AI_READ_LOCK),
		timeout=EMAIL_AI_READ_LOCK_TIMEOUT,
	)
	if not lock.acquire(blocking=False):
		return {"processed": False, "busy": True}
	try:
		return _fetch_one_email_and_ai_read()
	finally:
		lock.release()


def _fetch_one_email_and_ai_read():
	"""Internal implementation shared by the button and scheduled worker."""
	sync_result = _fetch_inbox()
	candidate = _next_ai_read_attachment()
	if not candidate:
		return {"sync": sync_result, "processed": False}

	from ez_accounting.www.forms.admin.finder import _ai_file_email_document

	result = _ai_file_email_document(candidate.name)
	return {
		"sync": sync_result,
		"processed": True,
		"attachment_name": candidate.name,
		"file_name": candidate.file_name,
		"subject": candidate.subject,
		**result,
	}


@frappe.whitelist()
def get_archive_settings():
	ensure_explicit_admin_page_access("admin_email")
	settings = _archive_settings()
	settings["accounts"] = frappe.get_all(
		"Email Account",
		filters={"enable_incoming": 1, "awaiting_password": 0},
		fields=["name", "email_id"],
		order_by="name asc",
	)
	return settings


@frappe.whitelist()
def save_archive_settings(email_account: str = "", sender_email: str = "", include_spam: int = 0):
	ensure_explicit_admin_page_access("admin_email")
	sender_email = _sender_address(sender_email)
	if not sender_email or "@" not in sender_email:
		frappe.throw(_("Enter a valid sender email address."))
	if email_account and not frappe.db.exists(
		"Email Account", {"name": email_account, "enable_incoming": 1, "awaiting_password": 0}
	):
		frappe.throw(_("Select an enabled incoming email account."))
	settings = frappe.get_single("Email Attachment Settings")
	settings.email_account = email_account or ""
	settings.sender_email = sender_email
	settings.include_spam = 1 if int(include_spam or 0) else 0
	settings.save(ignore_permissions=True)
	return _archive_settings()


@frappe.whitelist()
def get_emails(direction: str = "Received", search: str = "", start: int = 0, page_length: int = 100):
	ensure_explicit_admin_page_access("admin_email")
	direction = direction if direction in ("Received", "Sent") else "Received"
	start = max(int(start or 0), 0)
	page_length = min(max(int(page_length or 100), 1), 200)
	search = (search or "").strip()
	if direction == "Sent":
		values = {"search": f"%{search}%", "start": start, "page_length": page_length}
		search_condition = ""
		if search:
			search_condition = """AND (eq.sender LIKE %(search)s OR eq.message LIKE %(search)s
				OR EXISTS (SELECT 1 FROM `tabEmail Queue Recipient` sqr
					WHERE sqr.parent = eq.name AND sqr.recipient LIKE %(search)s))"""
		rows = frappe.db.sql(
			f"""
			SELECT eq.name, eq.message AS raw_message, eq.sender,
				GROUP_CONCAT(eqr.recipient ORDER BY eqr.idx SEPARATOR ', ') AS recipients,
				eq.creation AS communication_date,
				CASE WHEN IFNULL(eq.attachments, '') NOT IN ('', '[]') THEN 1 ELSE 0 END AS has_attachment,
				eq.status AS delivery_status
			FROM `tabEmail Queue` eq
			LEFT JOIN `tabEmail Queue Recipient` eqr ON eqr.parent = eq.name
			WHERE 1=1 {search_condition}
			GROUP BY eq.name
			ORDER BY eq.creation DESC
			LIMIT %(start)s, %(page_length)s
			""",
			values,
			as_dict=True,
		)
		for row in rows:
			try:
				subject = Parser(policy=policy.default).parsestr(row.pop("raw_message") or "").get("Subject")
				row["subject"] = str(make_header(decode_header(str(subject)))) if subject else _("(Outgoing Email)")
			except Exception:
				row.pop("raw_message", None)
				row["subject"] = _("(Outgoing Email)")
		return rows
	filters = {
		"communication_medium": "Email",
		"sent_or_received": direction,
	}
	or_filters = None
	if search:
		or_filters = {
			"subject": ["like", f"%{search}%"],
			"sender": ["like", f"%{search}%"],
			"recipients": ["like", f"%{search}%"],
		}
	rows = frappe.get_all(
		"Communication",
		filters=filters,
		or_filters=or_filters,
		fields=["name", "subject", "sender", "recipients", "communication_date", "has_attachment", "delivery_status"],
		order_by="communication_date desc",
		start=start,
		page_length=page_length,
	)
	return rows


@frappe.whitelist()
def get_archived_attachments():
	ensure_explicit_admin_page_access("admin_email")
	return frappe.get_all(
		"Email Attachments",
		fields=["name", "email_date", "sender", "subject", "file_name", "attachment", "source_communication"],
		order_by="email_date desc, creation desc",
		limit_page_length=500,
	)
