import frappe
from frappe.model.document import Document

from ez_accounting.file_access import allowed_companies


class FormsFileRecord(Document):
	def validate(self):
		if not frappe.db.exists("Company", self.company):
			frappe.throw("请选择有效公司")
		allowed = allowed_companies()
		if allowed is not None and self.company not in allowed:
			frappe.throw("无权访问该公司", frappe.PermissionError)
