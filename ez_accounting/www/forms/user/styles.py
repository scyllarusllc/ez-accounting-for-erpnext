# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import frappe
from frappe import _

from ez_accounting.api import ensure_style_admin

no_cache = 1


def get_context(context):
	ensure_style_admin()

	context.body_class = "forms-portal-dark"
	context.nav_active = "styles"
	context.title = _("Custom Styles")
	return context


def _reject_style_close_tag(custom_css: str):
	"""See forms_nav.html — the System Default style's CSS is rendered
	unescaped (`| safe`) inside a <style> tag on every Forms page (CSS is
	full of characters like '>' and '&' that HTML-autoescaping would
	otherwise mangle). A literal "</style" would close that tag early and let
	whatever follows run as raw HTML/script on every visitor's page; there's
	no legitimate CSS reason to ever write that string, so it's rejected
	outright for every style, not just whichever one happens to be default
	right now — any of them could be promoted to default later.
	"""
	if "</style" in (custom_css or "").lower():
		frappe.throw(_('Custom CSS can\'t contain "</style" — remove it and try again.'))


@frappe.whitelist()
def get_styles():
	ensure_style_admin()
	default_style = frappe.db.get_single_value("Forms Style Settings", "default_style")
	styles = frappe.get_all(
		"Forms Style", fields=["name", "style_name", "custom_css"], order_by="style_name asc"
	)
	for style in styles:
		style["is_default"] = style.name == default_style
	return {"styles": styles, "default_style": default_style}


@frappe.whitelist()
def create_style(style_name: str):
	ensure_style_admin()

	style_name = (style_name or "").strip()
	if not style_name:
		frappe.throw(_("Enter a name for the style."))
	if frappe.db.exists("Forms Style", style_name):
		frappe.throw(_("A style named {0} already exists.").format(style_name))

	doc = frappe.get_doc({"doctype": "Forms Style", "style_name": style_name, "custom_css": ""})
	doc.insert()
	frappe.db.commit()

	return {"name": doc.name}


@frappe.whitelist()
def update_style_css(style_name: str, custom_css: str = ""):
	ensure_style_admin()
	_reject_style_close_tag(custom_css)

	doc = frappe.get_doc("Forms Style", style_name)
	doc.custom_css = custom_css or ""
	doc.save()
	frappe.db.commit()

	return {"ok": True}


@frappe.whitelist()
def set_default_style(style_name: str = ""):
	"""style_name="" (or omitted) clears the System Default — every Forms
	page falls back to the portal's own shipped look with no override.
	"""
	ensure_style_admin()

	if style_name and not frappe.db.exists("Forms Style", style_name):
		frappe.throw(_("{0} is not a valid style.").format(style_name))

	frappe.db.set_single_value("Forms Style Settings", "default_style", style_name or None)
	frappe.db.commit()

	return {"default_style": style_name or None}


@frappe.whitelist()
def delete_style(style_name: str):
	ensure_style_admin()

	current_default = frappe.db.get_single_value("Forms Style Settings", "default_style")
	if style_name == current_default:
		frappe.throw(
			_("{0} is the current System Default — set a different style as default first.").format(
				style_name
			)
		)

	frappe.delete_doc("Forms Style", style_name)
	frappe.db.commit()

	return {"ok": True}
