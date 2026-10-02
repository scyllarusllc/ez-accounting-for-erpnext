# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document

from ez_accounting.permissions import CATALOG_KEYS, GATE_KEYS, GATE_ROLE


class FormsUserAccess(Document):
	"""Restricts one forms-portal user to a subset of header pages, and
	optionally a set of companies. Enforced centrally in
	forms.website_context.update_website_context(). See forms.permissions.

	Side effects this controller keeps in sync (and only these — it never
	touches doctype-level create/submit permissions or transaction roles):
	  - a Company User Permission (User -> Company) for each row in `companies`
	  - the user's default company (kept inside the `companies` set)
	  - the "Ez Accounting Admin" gate role on `user` (add-only unless this record
	    is the one that added it, tracked via `gate_role_added`)
	"""

	def validate(self):
		if not frappe.db.exists("User", self.user):
			frappe.throw(_("{0} is not a user.").format(self.user))
		if "System Manager" in frappe.get_roles(self.user):
			frappe.throw(_("{0} is a System Manager — their portal access can't be restricted here.").format(self.user))
		if self.user in ("Administrator", "Guest"):
			frappe.throw(_("{0} can't be given a Forms User Access record.").format(self.user))

		# Dedup + drop unknown keys (the console already validates, this is a
		# safety net — never accumulate rows across saves).
		seen = []
		for row in self.pages:
			key = (row.page or "").strip()
			if key and key in CATALOG_KEYS and key not in seen:
				seen.append(key)
		self.set("pages", [{"page": k} for k in seen])

		# Same for companies: dedup, drop blanks and anything that isn't a
		# real Company.
		seen_c = []
		for row in self.companies:
			company = (row.company or "").strip()
			if company and company not in seen_c and frappe.db.exists("Company", company):
				seen_c.append(company)
		self.set("companies", [{"company": c} for c in seen_c])

	def on_update(self):
		self._sync_companies()
		self._sync_gate_role()

	def on_trash(self):
		self._clear_companies()
		if self.gate_role_added:
			_remove_role(self.user, GATE_ROLE)

	# --- Companies (User Permission + default) ---

	@property
	def _companies(self) -> list[str]:
		return [row.company for row in self.companies if row.company]

	def _synced(self) -> list[str]:
		try:
			return json.loads(self.synced_companies) if self.synced_companies else []
		except (ValueError, TypeError):
			return []

	def _sync_companies(self):
		desired = self._companies
		synced = self._synced()

		if set(desired) != set(synced):
			for company in synced:
				if company not in desired:
					_delete_company_user_permission(self.user, company)
			for company in desired:
				if company not in synced:
					_ensure_company_user_permission(self.user, company)
			# A single permitted company is safe to flag is_default on its
			# User Permission (ERPNext prefills from it); with several, none is
			# — the active one is driven by the user default below instead.
			_set_company_user_permission_default(self.user, desired[0] if len(desired) == 1 else None)
			self.db_set("synced_companies", json.dumps(desired), update_modified=False)

		self._sync_default_company(desired, synced)

	def _sync_default_company(self, desired: list[str], synced: list[str]):
		current = frappe.defaults.get_user_default("company", user=self.user)
		if desired:
			if current not in desired:
				frappe.defaults.set_user_default("company", desired[0], user=self.user)
		elif current in synced:
			# This record no longer restricts company — only clear a default
			# it was responsible for, never one the admin set up elsewhere.
			frappe.defaults.clear_user_default("company", user=self.user)

	def _clear_companies(self):
		synced = self._synced()
		for company in synced:
			_delete_company_user_permission(self.user, company)
		if frappe.defaults.get_user_default("company", user=self.user) in synced:
			frappe.defaults.clear_user_default("company", user=self.user)

	# --- Gate role ---

	def _sync_gate_role(self):
		"""The user needs GATE_ROLE only while this record grants them a page
		whose own get_context() calls ensure_admin(). Add it when needed;
		remove it when no longer needed *and only if this record was what
		added it* (`gate_role_added`) — never strip a role the admin set up
		outside this console."""
		needs_gate = any(row.page in GATE_KEYS for row in self.pages)
		has_role = GATE_ROLE in frappe.get_roles(self.user)
		if needs_gate and not has_role:
			_add_role(self.user, GATE_ROLE)
			self.db_set("gate_role_added", 1, update_modified=False)
		elif not needs_gate and self.gate_role_added:
			_remove_role(self.user, GATE_ROLE)
			self.db_set("gate_role_added", 0, update_modified=False)


def _ensure_company_user_permission(user: str, company: str):
	if not frappe.db.exists(
		"User Permission", {"user": user, "allow": "Company", "for_value": company}
	):
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": user,
				"allow": "Company",
				"for_value": company,
				"is_default": 0,
				"apply_to_all_doctypes": 1,
			}
		).insert(ignore_permissions=True)


def _set_company_user_permission_default(user: str, company: str | None):
	"""Make exactly `company`'s Company User Permission the default one for
	`user` (and clear the flag on the others). `None` clears it on all."""
	for name, for_value in frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Company"},
		fields=["name", "for_value"],
		as_list=True,
	):
		should_be = 1 if (company and for_value == company) else 0
		if frappe.db.get_value("User Permission", name, "is_default") != should_be:
			frappe.db.set_value("User Permission", name, "is_default", should_be)


def _delete_company_user_permission(user: str, company: str):
	for name in frappe.get_all(
		"User Permission",
		filters={"user": user, "allow": "Company", "for_value": company},
		pluck="name",
	):
		frappe.delete_doc("User Permission", name, ignore_permissions=True, force=True)


def _add_role(user: str, role: str):
	u = frappe.get_doc("User", user)
	if role not in {r.role for r in u.roles}:
		u.append("roles", {"role": role})
		u.save(ignore_permissions=True)


def _remove_role(user: str, role: str):
	u = frappe.get_doc("User", user)
	remaining = [r for r in u.roles if r.role != role]
	if len(remaining) != len(u.roles):
		u.set("roles", remaining)
		u.save(ignore_permissions=True)
