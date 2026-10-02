"""Admin page: configure the "mini Martin" floating AI chat assistant
(forms_martin.js, site-wide on every /forms/* page) -- enabled toggle,
display name, avatar, model, timeouts, greeting, and system prompt. Backs
onto the "Forms AI Agent Settings" Single doctype, which forms.api's
get_mini_martin_config()/mini_martin_chat() read at request time (via
frappe.get_cached_doc, so a Save here takes effect on the very next chat
message with no restart needed).
"""

import frappe
from frappe import _

from ez_accounting.permissions import ensure_explicit_admin_page_access

no_cache = 1

PROVIDER_MODELS = {
	"Claude CLI": ["claude-haiku-4-5", "claude-sonnet-5", "claude-opus-5"],
	"Codex CLI": ["gpt-6-luna", "gpt-6-astra", "gpt-5.6-sol"],
}
EFFORT_OPTIONS = ["low", "medium", "high", "xhigh", "max"]

FIELDS = (
	"enabled", "admin_only", "allow_code_editing", "agent_name", "assistant_subtitle", "avatar", "provider", "model", "effort", "system_prompt",
	"greeting_message", "timeout_seconds", "max_history_turns",
)


def get_context(context):
	ensure_explicit_admin_page_access("admin_aiagent")
	context.body_class = "forms-portal-dark"
	context.nav_active = "aiagent"
	context.title = _("AI Assistant")
	return context


def _settings() -> dict:
	doc = frappe.get_single("Forms AI Agent Settings")
	row = doc.as_dict()
	return {field: row.get(field) for field in FIELDS}


@frappe.whitelist()
def get_settings():
	ensure_explicit_admin_page_access("admin_aiagent")
	data = _settings()
	data["provider_models"] = PROVIDER_MODELS
	data["effort_options"] = EFFORT_OPTIONS
	return data


@frappe.whitelist()
def save_settings(
	enabled: int = 0,
	admin_only: int = 0,
	allow_code_editing: int = 0,
	agent_name: str = "",
	assistant_subtitle: str = "",
	avatar: str = "",
	provider: str = "Claude CLI",
	model: str = "",
	effort: str = "medium",
	system_prompt: str = "",
	greeting_message: str = "",
	timeout_seconds: int = 45,
	max_history_turns: int = 20,
):
	ensure_explicit_admin_page_access("admin_aiagent")

	agent_name = (agent_name or "").strip()
	if not agent_name:
		frappe.throw(_("Assistant name is required."))
	if provider not in PROVIDER_MODELS:
		frappe.throw(_("Please select a valid CLI provider."))
	if model not in PROVIDER_MODELS[provider]:
		frappe.throw(_("Please select a valid model."))
	if effort not in EFFORT_OPTIONS:
		frappe.throw(_("Please select a valid thinking depth."))
	system_prompt = (system_prompt or "").strip()
	if not system_prompt:
		frappe.throw(_("System prompt is required."))

	def _bounded_int(value, default, lo, hi):
		try:
			value = int(value)
		except (TypeError, ValueError):
			value = default
		return max(lo, min(value, hi))

	doc = frappe.get_single("Forms AI Agent Settings")
	doc.enabled = 1 if int(enabled or 0) else 0
	doc.admin_only = 1 if int(admin_only or 0) else 0
	doc.allow_code_editing = 1 if int(allow_code_editing or 0) else 0
	doc.agent_name = agent_name
	doc.assistant_subtitle = (assistant_subtitle or "").strip()
	doc.avatar = (avatar or "").strip()
	doc.provider = provider
	doc.model = model
	doc.effort = effort
	doc.system_prompt = system_prompt
	doc.greeting_message = (greeting_message or "").strip()
	doc.timeout_seconds = _bounded_int(timeout_seconds, 45, 5, 1800)
	doc.max_history_turns = _bounded_int(max_history_turns, 20, 0, 100_000)
	doc.save(ignore_permissions=True)
	frappe.db.commit()

	return _settings()
