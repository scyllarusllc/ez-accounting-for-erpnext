"""Background jobs for the Forms application."""

import frappe


def enqueue_email_ai_read():
	"""Queue one email attachment for AI reading every scheduler interval."""
	frappe.enqueue(
		"ez_accounting.tasks.fetch_one_email_and_ai_read",
		queue="long",
		timeout=2100,
		job_id="forms-email-ai-read",
		deduplicate=True,
	)


def fetch_one_email_and_ai_read():
	"""Fetch mail and process at most one attachment in a long worker."""
	from ez_accounting.www.forms.admin.email import _fetch_one_email_and_ai_read_locked

	return _fetch_one_email_and_ai_read_locked()
