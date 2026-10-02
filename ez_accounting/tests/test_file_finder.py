import unittest
from unittest.mock import patch
from types import SimpleNamespace

import frappe

from ez_accounting.file_access import allowed_companies, has_permission, permission_query_conditions
from ez_accounting.www.forms.user import finder


class FinderPermissionTests(unittest.TestCase):
	def test_non_admin_without_company_grant_sees_nothing(self):
		with patch.object(frappe, "session", SimpleNamespace(user="reader")), patch("forms.file_access.permitted_docs", return_value=None):
			self.assertEqual(allowed_companies(), [])
			self.assertEqual(permission_query_conditions(), "1=0")
			self.assertFalse(has_permission(frappe._dict(company="Secret")))

	def test_search_scopes_both_keyword_and_filter_to_authorized_company(self):
		with patch.object(finder, "ensure_logged_in"), patch.object(finder, "allowed_companies", return_value=["A"]), \
			patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: True)), patch("frappe.get_all", return_value=[]) as query:
			finder.search(q="contract", company="A", category="Legal", document_from="2026-01-01")
			kwargs = query.call_args.kwargs
			self.assertIn(["company", "in", ["A"]], kwargs["filters"])
			self.assertIn(["company", "=", "A"], kwargs["filters"])
			self.assertEqual({row[0] for row in kwargs["or_filters"]}, {"title", "tags", "description", "extracted_content"})
			self.assertEqual(kwargs["limit_page_length"], 26)

	def test_cannot_request_another_company(self):
		with patch.object(finder, "ensure_logged_in"), patch.object(finder, "allowed_companies", return_value=["A"]), \
			patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: True)), patch.object(finder, "_", side_effect=lambda text: text), \
			patch.object(frappe, "throw", side_effect=frappe.PermissionError), patch("frappe.get_all") as query:
			with self.assertRaises(frappe.PermissionError):
				finder.search(company="B")
			query.assert_not_called()

	def test_admin_can_search_all_companies(self):
		with patch.object(finder, "ensure_logged_in"), patch.object(finder, "allowed_companies", return_value=None), \
			patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: True)), patch("frappe.get_all", return_value=[]) as query:
			finder.search()
			self.assertEqual(query.call_args.kwargs["filters"], [])

	def test_category_options_use_only_authorized_companies(self):
		def rows(doctype, **kwargs):
			if doctype == "Company":
				self.assertEqual(kwargs["filters"], {"name": ["in", ["A"]]})
				return ["A"]
			self.assertEqual(kwargs["filters"]["company"], ["in", ["A"]])
			return [frappe._dict(category="Legal")]
		with patch.object(finder, "ensure_logged_in"), patch.object(finder, "allowed_companies", return_value=["A"]), \
			patch.object(frappe, "session", SimpleNamespace(user="reader")), \
			patch.object(frappe, "db", SimpleNamespace(exists=lambda *args: True)), patch("frappe.get_all", side_effect=rows):
			self.assertEqual(finder.options()["categories"], ["Legal"])


if __name__ == "__main__":
	unittest.main()
