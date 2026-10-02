# Copyright (c) 2026, Eucon and contributors
# For license information, please see license.txt

import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import frappe
from frappe import _
from frappe.utils import add_to_date, flt, nowdate

from ez_accounting.ai_read_log import log_ai_read_start, log_ai_read_update

# Roles allowed to see the aggregated/admin views (both 990 and 941).
# "Ez Accounting Admin" is a custom website role created on install (see README).
ADMIN_ROLES = {"System Manager", "Ez Accounting Admin"}

FORM_941_FIELDS = [
	"number_of_employees",
	"wages_tips_compensation",
	"federal_income_tax_withheld",
	"taxable_social_security_wages",
	"taxable_social_security_tips",
	"taxable_medicare_wages",
	"taxable_wages_additional_medicare",
	"total_social_security_medicare_taxes",
	"total_taxes_before_adjustments",
	"total_deposits",
	"balance_due",
	"overpayment",
]

FORM_W2_EMPLOYEE_FIELDS = [
	"employee_name",
	"ssn_last4",
	"wages_tips_compensation",
	"federal_income_tax_withheld",
	"social_security_wages",
	"social_security_tax_withheld",
	"medicare_wages",
	"medicare_tax_withheld",
	"state",
	"state_wages",
	"state_income_tax",
]

FORM_1023_CHECK_FIELDS = [
	"articles_of_incorporation_filed",
	"bylaws_adopted",
	"ein_obtained",
	"dissolution_clause_included",
	"conflict_of_interest_policy_adopted",
]


def ensure_logged_in():
	if frappe.session.user == "Guest":
		frappe.throw(_("You must be logged in to access this resource."), frappe.PermissionError)


def ensure_admin():
	ensure_logged_in()
	if not ADMIN_ROLES.intersection(frappe.get_roles()):
		frappe.throw(_("You are not permitted to access the admin view."), frappe.PermissionError)


def is_admin_user(user: str | None = None) -> bool:
	"""Non-throwing version of ensure_admin()'s own check -- for gating a UI
	section (e.g. the Dashboard's Activity panel) that should render for
	admin-tier users and simply not appear for everyone else, rather than
	failing the whole page. The whitelisted endpoints that panel calls still
	use ensure_admin() itself as the real security boundary."""
	user = user or frappe.session.user
	if user == "Guest":
		return False
	return bool(ADMIN_ROLES.intersection(frappe.get_roles(user)))


# --- Real-time "who's online" -----------------------------------------------
# Frappe's own tabSessions.lastupdate is NOT a live signal -- Session.update()
# (frappe/sessions.py) only actually writes to the DB at most once every
# ~10 minutes per session regardless of how often that user's browser makes
# requests (a deliberate throttle, to avoid a DB write on every single page
# view). That's fine for "extend session expiry", but far too coarse for an
# "online now" indicator someone expects to update within moments of a tab
# opening or closing.
#
# So every /forms/* page's own JS (see forms_portal.js) instead calls ping()
# on a short interval while the tab is open -- a trivial whitelisted call
# that writes only to Redis (frappe.cache(), never the DB), recording "this
# user was here" with real, sub-minute freshness. get_online_users() reads
# that same hash and treats an entry older than ONLINE_PING_WINDOW_SECONDS as
# stale -- comfortably more than one missed ping's worth of slack (network
# hiccup, a slow request) without still counting someone as online minutes
# after they've actually left.
ONLINE_PING_CACHE_KEY = "forms_online_pings"
ONLINE_PING_WINDOW_SECONDS = 90


@frappe.whitelist()
def ping():
	"""Heartbeat -- called on an interval by every logged-in viewer's own
	browser (forms_portal.js) for as long as a /forms/* tab stays open.
	Cheap on purpose: one Redis hash-field write, no DB write, no response
	body to speak of."""
	ensure_logged_in()
	frappe.cache().hset(ONLINE_PING_CACHE_KEY, frappe.session.user, frappe.utils.now_datetime())
	return {"ok": True}


# "mini Martin" -- the floating AI assistant chat button (forms_martin.js) on
# every /forms/* page. An admin can select the local Claude CLI or Codex CLI,
# so there is no separate API key to manage. Claude runs with tools disabled;
# Codex runs ephemerally from an empty temporary directory in its read-only
# sandbox. This is a conversational chat available to any logged-in user.
#
# Configurable at /forms/admin/aiagent (ez_accounting.www.forms.admin.aiagent),
# backed by the "Forms AI Agent Settings" Single doctype.
# frappe.get_cached_doc() means a Save there takes effect on the very next
# chat message -- no restart needed (Frappe clears a Single's cache entry on
# save automatically).
MINI_MARTIN_MAX_URL_LENGTH = 500
MINI_MARTIN_MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024
MINI_MARTIN_ATTACHMENT_TTL_SECONDS = 1800
MINI_MARTIN_MAX_TIMEOUT_SECONDS = 1800
MINI_MARTIN_QUEUE_GRACE_SECONDS = 60


def _mini_martin_timeout(settings) -> int:
	"""Use the admin setting consistently for both the CLI and queue job."""
	return max(5, min(int(settings.timeout_seconds or 45), MINI_MARTIN_MAX_TIMEOUT_SECONDS))


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
	"""Preserve whatever the CLI emitted before Python stopped it."""
	parts = []
	for value in (exc.stdout, exc.stderr):
		if isinstance(value, bytes):
			value = value.decode("utf-8", errors="replace")
		if value and str(value).strip():
			parts.append(str(value).strip())
	return "\n\n".join(parts)[-20_000:]


@frappe.whitelist()
def upload_mini_martin_attachment():
	"""Accepts one multipart file for the NEXT mini Martin chat message.
	Written to its own throwaway directory on disk -- never the site's
	shared private/files/ store, which holds every other user's uploads too
	-- and referenced by a short-lived, single-use token rather than the raw
	path, so the browser never needs (or gets) filesystem access. The token
	expires in MINI_MARTIN_ATTACHMENT_TTL_SECONDS if never consumed by
	mini_martin_chat(), and is deleted immediately once it is (see that
	function's own attachment handling) -- either way this never accumulates
	on disk.
	"""
	ensure_logged_in()

	files = getattr(frappe.request, "files", None)
	uploaded = files.get("file") if files else None
	if not uploaded:
		frappe.throw(_("No file was uploaded."))

	content = uploaded.stream.read()
	if len(content) > MINI_MARTIN_MAX_ATTACHMENT_BYTES:
		frappe.throw(
			_("That file is too large (max {0} MB).").format(MINI_MARTIN_MAX_ATTACHMENT_BYTES // (1024 * 1024))
		)

	filename = uploaded.filename or "upload"

	from mimetypes import guess_type

	from frappe.handler import ALLOWED_MIMETYPES

	content_type = (getattr(uploaded, "mimetype", None) or guess_type(filename)[0] or "").lower()
	is_allowed = content_type in ALLOWED_MIMETYPES or (
		content_type.startswith("image/") and content_type != "image/svg+xml"
	)
	if not is_allowed:
		frappe.throw(_("You can only attach an image, a PDF, or a text/Office document."))

	work_dir = tempfile.mkdtemp(prefix="mini-martin-upload-")
	safe_name = os.path.basename(filename) or "attachment"
	path = os.path.join(work_dir, safe_name)
	with open(path, "wb") as f:
		f.write(content)

	token = frappe.generate_hash(length=24)
	frappe.cache().set_value(
		f"mini_martin_attachment:{token}",
		{"user": frappe.session.user, "path": path, "work_dir": work_dir, "file_name": safe_name},
		expires_in_sec=MINI_MARTIN_ATTACHMENT_TTL_SECONDS,
	)
	return {"attachment_id": token, "file_name": safe_name}


def _mini_martin_settings():
	return frappe.get_cached_doc("Forms AI Agent Settings")


@frappe.whitelist()
def get_mini_martin_config():
	"""Non-sensitive subset of the settings forms_martin.js needs to decide
	whether to render itself and how to label/brand what it renders --
	system_prompt stays server-side only, never sent to the browser."""
	ensure_logged_in()
	settings = _mini_martin_settings()
	return {
		"enabled": bool(settings.enabled) and (not settings.admin_only or is_admin_user()),
		"agent_name": settings.agent_name or "mini Martin",
		"avatar": settings.avatar or "/files/mini-martin.jpg",
		"greeting_message": settings.greeting_message or "Hi, I'm mini Martin — ask me anything!",
		"provider": settings.provider or "Claude CLI",
		"assistant_subtitle": (settings.assistant_subtitle or "Virtual Martin · {provider}").replace(
			"{provider}", settings.provider or "Claude CLI"
		),
	}


@frappe.whitelist()
def get_mini_martin_history(limit: int = 50):
	"""Return this login's Mini Martin conversation from the AI Logs.

	The server fixes both reference fields from the authenticated session;
	the browser cannot request another user's conversation.
	"""
	ensure_logged_in()
	limit = max(1, min(int(limit or 50), 100))
	rows = frappe.get_all(
		"Forms AI Read Log",
		filters={
			"reference_doctype": "User",
			"reference_name": frappe.session.user,
			"action": "Mini Martin Chat",
			"status": "Success",
		},
		fields=["input_text", "note", "output_text", "creation", "modified"],
		order_by="creation desc",
		limit_page_length=limit,
	)
	messages = []
	for row in reversed(rows):
		user_text = (row.input_text or row.note or "").strip()
		assistant_text = (row.output_text or "").strip()
		if user_text:
			messages.append({"role": "user", "content": user_text, "timestamp": row.creation})
		if assistant_text:
			messages.append({"role": "assistant", "content": assistant_text, "timestamp": row.modified})
	return {"messages": messages}


@frappe.whitelist()
def mini_martin_chat(
	message: str, history=None, url: str = "", attachment_id: str = "", reply_language: str = "",
	_background_task_id: str = "", _log_name: str = "",
	_attachment_path: str = "", _attachment_name: str = "", _attachment_work_dir: str = "",
):
	"""One chat turn with mini Martin. Stateless on the server (like every
	CLI call) -- the caller (forms_martin.js) resends the recent
	conversation each time, capped at the configured max history turns, and
	this just folds it into the prompt text as a transcript. `url` is the
	page the user was on when they asked -- prefixed onto their question so
	mini Martin has some idea what they're looking at.

	Every call runs through the background-task/poll flow (see
	_run_mini_martin_task()/get_mini_martin_task() below), not just
	code-editing ones -- a plain Claude chat used to run synchronously inside
	the HTTP request, which meant any reply slower than this site's nginx
	`proxy_read_timeout` (120s on your-site.example.com) got its connection killed with
	a bare non-JSON 504 before Frappe could return (or even log) a real
	error, surfacing to the user as a generic "Request failed". Enqueuing
	unconditionally means no single chat message is ever bound by that
	window again.

	Every call is logged to Forms AI Read Log (reference "User" / the session
	user) for the admin AI Logs page -- a "Running" row the instant it's
	enqueued (see log_ai_read_start(), called by the top-level whitelisted
	call below), updated to Success/Error once this finishes. That start-row
	is what makes a task that dies without ever finishing (worker killed,
	OOM, a code-editing task restarting its own worker process via
	live_reload) show up as a stuck "Running" entry instead of leaving no
	trace at all.
	"""
	ensure_logged_in()
	settings = _mini_martin_settings()
	if settings.admin_only and not is_admin_user():
		frappe.throw(_("The assistant is available to administrators only."), frappe.PermissionError)

	user = frappe.session.user
	provider = settings.provider or "Claude CLI"
	model = settings.model or ("gpt-6-luna" if provider == "Codex CLI" else "claude-haiku-4-5")

	if not _background_task_id:
		timeout_seconds = _mini_martin_timeout(settings)
		task_ttl = timeout_seconds + 600
		attachment_path, attachment_name, attachment_work_dir = "", "", ""
		if attachment_id:
			key = f"mini_martin_attachment:{attachment_id}"
			data = frappe.cache().get_value(key)
			if not data or data.get("user") != user:
				frappe.throw(_("That attachment was not found or has expired. Please attach the file again."))
			attachment_path = data.get("path") or ""
			attachment_name = data.get("file_name") or ""
			attachment_work_dir = data.get("work_dir") or ""
			frappe.cache().delete_value(key)  # single-use token

		task_id = frappe.generate_hash(length=24)
		log_name = log_ai_read_start(
			"User", user, "Mini Martin Chat", model, input_text=(message or "").strip()
		)
		frappe.cache().set_value(
			f"mini_martin_task:{task_id}",
			{"user": user, "status": "queued", "timeout_seconds": timeout_seconds},
			expires_in_sec=task_ttl,
		)
		frappe.enqueue(
			"ez_accounting.api._run_mini_martin_task",
			queue="long",
			timeout=timeout_seconds + MINI_MARTIN_QUEUE_GRACE_SECONDS,
			task_id=task_id,
			log_name=log_name,
			user=user,
			message=message,
			history=history,
			url=url,
			attachment_path=attachment_path,
			attachment_name=attachment_name,
			attachment_work_dir=attachment_work_dir,
			reply_language=(reply_language or "").strip()[:100],
		)
		return {"pending": True, "task_id": task_id}

	if not settings.enabled:
		log_ai_read_update(_log_name, "Error", note="mini Martin is currently turned off.")
		return {"reply": _("mini Martin is currently turned off. Please check back later.")}

	effort = settings.effort or "medium"
	timeout_seconds = _mini_martin_timeout(settings)
	max_history_turns = max(0, min(int(settings.max_history_turns or 0), 100))
	system_prompt = settings.system_prompt or ""
	user_roles = frappe.get_roles(user)
	user_full_name = frappe.db.get_value("User", user, "full_name") or user

	can_edit_code = (
		provider == "Codex CLI"
		and bool(settings.allow_code_editing)
		and bool(ADMIN_ROLES.intersection(user_roles))
	)
	started = time.monotonic()

	def log(status, payload=None, note="", output_text=None):
		log_ai_read_update(
			_log_name, status, payload=payload, note=note, output_text=output_text,
			duration_ms=int((time.monotonic() - started) * 1000),
		)

	message = (message or "").strip()
	if not message:
		frappe.throw(_("Message is required."))
	url = (url or "").strip()[:MINI_MARTIN_MAX_URL_LENGTH]

	if isinstance(history, str):
		try:
			history = json.loads(history) if history else []
		except ValueError:
			history = []
	if not isinstance(history, list):
		history = []
	history = history[-max_history_turns:] if max_history_turns else []

	transcript_lines = []
	for turn in history:
		if not isinstance(turn, dict):
			continue
		role = "User" if turn.get("role") == "user" else "mini Martin"
		text = str(turn.get("content") or "")
		if text:
			transcript_lines.append(f"{role}: {text}")

	reply_language = (reply_language or "").strip() or "Chinese (中文)"
	prompt = system_prompt + "\n\n"
	prompt += (
		f'Always reply in {reply_language}, regardless of what language the user writes their message in, '
		"unless they explicitly ask you to switch languages for this conversation.\n\n"
	)
	prompt += (
		"Authenticated Forms user:\n"
		f"- User ID / email: {user}\n"
		f"- Full name: {user_full_name}\n"
		f"- Roles: {', '.join(user_roles) if user_roles else 'None'}\n"
		"Use this identity when the user asks who is signed in or when their role is relevant.\n\n"
	)
	if can_edit_code:
		prompt += (
			"You are running inside /home/frappe/frappe/frappe-bench with workspace write access. "
			"You may inspect and edit the application source code when the user asks. Follow AGENTS.md, "
			"make the requested change completely, validate it, and report the files changed. A Forms URL "
			"such as /forms/admin/aiagent maps to code in the installed Forms app; edit its source rather "
			"than trying to publish content directly to the URL.\n\n"
		)
	if transcript_lines:
		prompt += "Conversation so far:\n" + "\n".join(transcript_lines) + "\n\n"

	# Attachment handling: the file's content is read here, in Python, and
	# folded straight into the message -- as inline text for a text file, or
	# (Claude CLI only, see below) as a real vision image block for an image.
	# Neither CLI is ever granted a file-reading TOOL or filesystem access of
	# its own for this -- an image block is content, exactly like the prompt
	# text itself, not a new capability; Codex CLI attachments and non-image
	# binary files (PDF, Word, etc.) still just get a plain "can't read that"
	# note, since there's no equivalent content-block path wired up for them.
	image_base64 = ""
	image_media_type = ""
	if _attachment_path and os.path.exists(_attachment_path):
		safe_name = _attachment_name or os.path.basename(_attachment_path) or "attachment"
		from mimetypes import guess_type

		guessed_type = (guess_type(safe_name)[0] or "").lower()
		is_image = guessed_type.startswith("image/") and guessed_type != "image/svg+xml"
		try:
			if is_image and provider != "Codex CLI":
				with open(_attachment_path, "rb") as attachment_file:
					raw = attachment_file.read()
				import base64 as _base64

				image_base64 = _base64.b64encode(raw).decode("ascii")
				image_media_type = guessed_type
				prompt += f'\n\n[The user attached an image named "{safe_name}" -- see the image below.]\n'
			else:
				with open(_attachment_path, "rb") as attachment_file:
					raw = attachment_file.read(200_000)
				try:
					text = raw.decode("utf-8")
				except UnicodeDecodeError:
					text = None
				if text and text.strip():
					snippet = text[:12000]
					if len(text) > 12000:
						snippet += "\n...(truncated)"
					prompt += (
						f'\n\n[The user attached a text file named "{safe_name}". Its contents:]\n'
						f"---\n{snippet}\n---\n"
					)
				elif is_image:
					prompt += (
						f'\n\n[The user attached an image named "{safe_name}", but mini Martin can only see '
						"images when using the Claude CLI provider (see AI Agent settings) -- it can't read "
						"this one here.]\n"
					)
				else:
					prompt += (
						f'\n\n[The user attached a file named "{safe_name}", but mini Martin can only read '
						"plain-text files (e.g. .txt, .csv, .md, .json, code files) or images (with Claude CLI) "
						"through chat -- this looks like a binary or formatted file (PDF, Word, etc.) it can't "
						"read here.]\n"
					)
		except OSError:
			prompt += f'\n\n[The user attached a file named "{safe_name}", but it could not be read.]\n'
		finally:
			if _attachment_work_dir:
				shutil.rmtree(_attachment_work_dir, ignore_errors=True)

	user_line = f"at {url} {message}" if url else message
	prompt += f"\nUser: {user_line}\nmini Martin:"

	if provider == "Codex CLI":
		binary = shutil.which("codex") or "codex"
		with tempfile.TemporaryDirectory(prefix="mini-martin-") as workdir:
			output_file = f"{workdir}/reply.txt"
			codex_workdir = "/home/frappe/frappe/frappe-bench" if can_edit_code else workdir
			cmd = [
				binary, "exec", "--ephemeral", "--skip-git-repo-check",
			]
			if can_edit_code:
				# This host's bundled Bubblewrap makes the bench read-only even with
				# workspace-write. The explicit admin role gate above is therefore
				# required before allowing Codex to run as the limited `frappe` OS user.
				cmd.append("--dangerously-bypass-approvals-and-sandbox")
			else:
				cmd.extend(["--sandbox", "read-only"])
			cmd.extend([
				"--color", "never", "-C", codex_workdir, "--model", model,
				"-c", f'model_reasoning_effort="{effort}"',
				"--output-last-message", output_file, "-",
			])
			try:
				proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout_seconds)
			except FileNotFoundError:
				log("Error", note="The local `codex` CLI isn't available on this server.")
				frappe.throw(_("The local `codex` CLI isn't available on this server."))
			except subprocess.TimeoutExpired as exc:
				partial_output = _timeout_output(exc)
				try:
					with open(output_file, encoding="utf-8") as output:
						partial_output = output.read().strip() or partial_output
				except OSError:
					pass
				log("Error", note=f"Timed out after {timeout_seconds} seconds.", output_text=partial_output)
				frappe.throw(_("mini Martin took too long to respond. Please try again."))
			reply = ""
			if proc.returncode == 0:
				try:
					with open(output_file, encoding="utf-8") as output:
						reply = output.read().strip()
				except OSError:
					pass
		payload = {"provider": provider, "model": model, "result": reply}
	else:
		binary = shutil.which("claude") or "claude"
		if image_base64:
			# Real vision, via the CLI's stream-json input protocol -- the
			# image travels as a normal content block alongside the prompt
			# text, exactly like the Messages API's own image content block.
			# This is still zero additional TOOL access (--allowedTools ""
			# stays empty): the model can see the picture in the same sense
			# it can already "see" the prompt text, nothing more.
			stdin_payload = json.dumps({
				"type": "user",
				"message": {
					"role": "user",
					"content": [
						{"type": "text", "text": prompt},
						{"type": "image", "source": {"type": "base64", "media_type": image_media_type, "data": image_base64}},
					],
				},
			}) + "\n"
			cmd = [
				binary, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
				"--model", model, "--effort", effort, "--allowedTools", "", "--permission-prompts", "none",
			]
		else:
			stdin_payload = None
			cmd = [
				binary, "-p", prompt, "--output-format", "json", "--model", model,
				"--effort", effort, "--allowedTools", "", "--permission-prompts", "none",
			]
		try:
			proc = subprocess.run(cmd, input=stdin_payload if stdin_payload is not None else None,
				capture_output=True, text=True, timeout=timeout_seconds)
		except FileNotFoundError:
			log("Error", note="The local `claude` CLI isn't available on this server.")
			frappe.throw(_("The local `claude` CLI isn't available on this server."))
		except subprocess.TimeoutExpired as exc:
			log(
				"Error", note=f"Timed out after {timeout_seconds} seconds.",
				output_text=_timeout_output(exc),
			)
			frappe.throw(_("mini Martin took too long to respond. Please try again."))
		payload = {}
		if proc.returncode == 0:
			if image_base64:
				# stream-json output is one JSON object per line -- the final
				# "result" line carries the same shape (result/usage/cost) as
				# the plain --output-format json response parsed below.
				for line in reversed(proc.stdout.splitlines()):
					line = line.strip()
					if not line:
						continue
					try:
						candidate = json.loads(line)
					except ValueError:
						continue
					if candidate.get("type") == "result":
						payload = candidate
						break
			else:
				try:
					payload = json.loads(proc.stdout)
				except ValueError:
					payload = {}
		reply = (payload.get("result") or "").strip()

	if proc.returncode != 0:
		frappe.log_error(title="ez_accounting.api.mini_martin_chat", message=(proc.stderr or "")[:3000])
		log("Error", note=(proc.stderr or "").strip()[:500])
		if "not supported when using Codex with a ChatGPT account" in (proc.stderr or ""):
			frappe.throw(_("The selected Codex model isn't available for this server's ChatGPT account. Choose another model in AI Agent settings."))
		frappe.throw(_("mini Martin couldn't respond just now."))

	if not reply:
		frappe.log_error(title="ez_accounting.api.mini_martin_chat", message=json.dumps(payload)[:3000])
		log("Error", payload=payload, note="mini Martin didn't have anything to say.")
		frappe.throw(_("mini Martin didn't have anything to say. Please try again."))

	log("Success", payload=payload, note=message[:200], output_text=reply)
	return {"reply": reply}


def _run_mini_martin_task(
	task_id: str, log_name: str, user: str, message: str, history=None, url: str = "",
	attachment_path: str = "", attachment_name: str = "", attachment_work_dir: str = "",
	reply_language: str = "",
):
	"""Run every mini Martin chat turn outside the web process -- not just
	write-enabled Codex work. Two independent reasons this queue hop exists:

	1. A plain synchronous chat used to run inline in the HTTP request, bound
	   by this site's nginx `proxy_read_timeout`; queuing means no chat is
	   ever killed mid-flight by that window (see mini_martin_chat()'s own
	   docstring).
	2. Editing Python files (when Codex has bench write access) restarts
	   Frappe's development web worker via live_reload. A queue worker is
	   unaffected by that restart, and the browser recovers the result by
	   polling get_mini_martin_task() regardless of what happened to the web
	   worker in the meantime.

	`log_name` is the "Running" Forms AI Read Log row mini_martin_chat()
	already inserted before enqueuing this -- passed through so the eventual
	outcome updates that same row instead of leaving it stuck, even for a
	failure this function's own `except` catches that mini_martin_chat()'s
	internal error handling never got a chance to log itself.
	"""
	key = f"mini_martin_task:{task_id}"
	frappe.set_user(user)
	timeout_seconds = _mini_martin_timeout(_mini_martin_settings())
	task_ttl = timeout_seconds + 600
	frappe.cache().set_value(
		key,
		{"user": user, "status": "running", "started_at": time.time(), "timeout_seconds": timeout_seconds},
		expires_in_sec=task_ttl,
	)
	try:
		result = mini_martin_chat(
			message, history, url, reply_language=reply_language,
			_background_task_id=task_id, _log_name=log_name,
			_attachment_path=attachment_path, _attachment_name=attachment_name,
			_attachment_work_dir=attachment_work_dir,
		)
		frappe.cache().set_value(
			key,
			{"user": user, "status": "complete", "reply": result.get("reply") or ""},
			expires_in_sec=task_ttl,
		)
	except Exception as exc:
		frappe.log_error(title="ez_accounting.api._run_mini_martin_task", message=frappe.get_traceback())
		# Safety net: mini_martin_chat() updates the log row itself before
		# throwing on every error path it knows about. Only update a row still
		# marked Running here; otherwise this fallback would erase partial CLI
		# output that the timeout handler already saved.
		if log_name and frappe.db.get_value("Forms AI Read Log", log_name, "status") == "Running":
			log_ai_read_update(log_name, "Error", note=str(exc)[:500])
		# Second safety net: mini_martin_chat() only cleans up the attachment
		# work dir once it reaches the attachment-handling code -- an error
		# thrown earlier (disabled, empty message) would otherwise leak it.
		# Harmless no-op if it's already gone.
		if attachment_work_dir:
			shutil.rmtree(attachment_work_dir, ignore_errors=True)
		frappe.cache().set_value(
			key,
			{"user": user, "status": "error", "error": str(exc)[:500]},
			expires_in_sec=task_ttl,
		)


@frappe.whitelist()
def get_mini_martin_task(task_id: str):
	ensure_logged_in()
	data = frappe.cache().get_value(f"mini_martin_task:{(task_id or '').strip()}")
	if not data or data.get("user") != frappe.session.user:
		frappe.throw(_("Mini Martin task was not found."))
	return {key: value for key, value in data.items() if key != "user"}


def get_online_users(window_seconds: int | None = None) -> list[dict]:
	"""Every user whose ping() landed within the last `window_seconds` --
	real-time, unlike a tabSessions-based query (see this section's own
	header comment). Returns {"user", "last_seen"} dicts, newest first."""
	window_seconds = window_seconds or ONLINE_PING_WINDOW_SECONDS
	pings = frappe.cache().hgetall(ONLINE_PING_CACHE_KEY) or {}
	cutoff = add_to_date(frappe.utils.now_datetime(), seconds=-window_seconds)

	online = []
	for user, last_seen in pings.items():
		# RedisWrapper.hgetall() only unpickles values, not hash field names --
		# a field name written via hset() comes back as raw bytes here.
		if isinstance(user, bytes):
			user = user.decode("utf-8")
		if not user or user == "Guest":
			continue
		last_seen = frappe.utils.get_datetime(last_seen)
		if last_seen and last_seen >= cutoff:
			online.append({"user": user, "last_seen": last_seen})

	online.sort(key=lambda r: r["last_seen"], reverse=True)
	return online


# --- ERPNext User Permission support --------------------------------------
# Every portal query in this app runs through frappe.get_all() +
# doc.save(ignore_permissions=True), which skip Frappe's permission layer
# entirely -- so a user restricted in Desk (/desk/user-permission) to one
# Company or a subset of Customers/Suppliers/Items/Employees would otherwise
# still see and act on everything here. These helpers pull the same User
# Permission records Desk uses and let each list endpoint scope itself.
#
# Matches Desk semantics: Administrator and any user with no User Permission
# records are unrestricted; role (System Manager included) does not exempt a
# user. `applicable_for` is deliberately not honored -- any User Permission on
# a doctype restricts it everywhere in this portal.


def _forms_user_permissions() -> dict[str, list[str]]:
	"""{doctype: [permitted doc names]} for the session user. Empty dict means
	unrestricted. Nested-set descendants are already expanded by Frappe's own
	get_user_permissions() (unless the rule sets hide_descendants)."""
	from frappe.permissions import get_user_permissions

	out: dict[str, list[str]] = {}
	for doctype, rules in (get_user_permissions(frappe.session.user) or {}).items():
		values = [r.get("doc") for r in rules if r.get("doc")]
		if values:
			out[doctype] = values
	return out


def permitted_docs(doctype: str) -> list[str] | None:
	"""Doc names of `doctype` the session user is limited to via User
	Permission, or None when the user is not restricted on that doctype."""
	return _forms_user_permissions().get(doctype)


def is_company_restricted() -> bool:
	return permitted_docs("Company") is not None


def apply_permitted_filter(filters, doctype: str, fieldname: str = "name"):
	"""Add a `[fieldname, "in", permitted]` clause to a frappe.get_all filter
	set when the session user is User-Permission-restricted on `doctype`;
	return `filters` unchanged otherwise. Handles both the dict and the list
	filter shapes used across this app."""
	permitted = permitted_docs(doctype)
	if permitted is None:
		return filters
	if isinstance(filters, dict):
		return {**filters, fieldname: ["in", permitted]}
	return [*(filters or []), [fieldname, "in", permitted]]


# Editing the portal's site-wide custom CSS is a distinct, higher-trust
# capability than the general portal-admin roles above — it changes what
# every visitor sees, not just this admin's own data — so it's gated to
# System Manager specifically rather than the whole ADMIN_ROLES set.
STYLE_ADMIN_ROLE = "System Manager"


def is_system_manager(user: str | None = None) -> bool:
	"""True for a full System Manager — the line this app draws for "trusted
	to see everything" (vs. a scoped portal user). Also what gates
	/forms/admin/permissions and the "Show all" escape hatches on the
	Customers / Item Accounts pages."""
	return "System Manager" in frappe.get_roles(user or frappe.session.user)


def is_style_admin() -> bool:
	return STYLE_ADMIN_ROLE in frappe.get_roles()


def ensure_style_admin():
	ensure_logged_in()
	if not is_style_admin():
		frappe.throw(_("You are not permitted to edit the portal's custom style."), frappe.PermissionError)


def get_forms_custom_css() -> str:
	"""The custom CSS override in effect for the current user on the Forms
	portal, checked in two tiers:

	1. This user's own personal pick (frappe.defaults.get_user_default, key
	   "forms_style") — set via /forms/user/me's "My Style" selector,
	   available to *every* logged-in user, not just System Managers,
	   since picking one of the already-authored styles carries none of the
	   risk that writing CSS does. Self-heals the same way
	   get_selected_company() does: a personal pick left over from a since-
	   deleted Forms Style is cleared rather than trusted.
	2. Otherwise, the System Default a System Manager set via /forms/user/
	   styles (forms_style_settings.default_style) — the same site-wide
	   fallback this function always resolved to before per-user styles
	   existed.

	Returns "" (the portal's shipped default look) if neither is set. Read
	role-agnostically throughout (frappe.db.get_value, not a permission-
	checked Document load) since the result applies to this render
	regardless of who can edit any of it; only *writing* a style's CSS or
	the System Default is gated (ensure_style_admin(), in styles.py) —
	writing *which* style a user personally picks is gated only to being
	logged in (set_my_forms_style(), below).
	"""
	user_style = frappe.defaults.get_user_default("forms_style")
	if user_style:
		if frappe.db.exists("Forms Style", user_style):
			return frappe.db.get_value("Forms Style", user_style, "custom_css") or ""
		frappe.defaults.clear_user_default("forms_style")

	default_style = frappe.db.get_single_value("Forms Style Settings", "default_style")
	if not default_style:
		return ""
	return frappe.db.get_value("Forms Style", default_style, "custom_css") or ""


@frappe.whitelist()
def log_client_error(context: str, message: str, stack: str | None = None, extra: str | None = None):
	"""Persist a browser-side JS error to the Error Log. The portal's generic
	fallback message ("Something went wrong. Check the Error Log for
	details." -- see extract_error_message() copied into most /forms/user/*
	pages) is shown whenever a caught error carries no `_server_messages`/
	`exception` -- typically a *client-side* exception (a bad record tripping
	up a page's own rendering code, a network hiccup, a bug in the shared
	multi-company fetch/XHR patch in forms_portal.js), which by itself leaves
	nothing in the Error Log at all despite what that message promises. Pages
	that render nontrivial data client-side call this from their own catch
	blocks so a real occurrence leaves an actual trace — who, on what page,
	the real error and stack — instead of only ever being visible (or missed)
	in one user's own devtools console.
	"""
	ensure_logged_in()
	title = f"Forms client error: {context}"[:140]
	try:
		path = frappe.request.path
	except Exception:
		path = ""
	body = f"User: {frappe.session.user}\nPath: {path}\n\n{message or ''}"
	if extra:
		body += f"\n\nExtra: {extra}"
	if stack:
		body += f"\n\nStack:\n{stack}"
	frappe.log_error(title=title, message=body)
	return {"logged": True}


@frappe.whitelist()
def get_forms_style_names() -> list[str]:
	"""Every Forms Style's name, for any logged-in user to pick their own
	from on /forms/user/me — unlike styles.py's get_styles() (System Manager
	only, includes each style's full CSS for editing), this is just names,
	safe for every user to read since picking an existing, already-authored
	style carries none of the risk that writing one does.
	"""
	ensure_logged_in()
	return frappe.get_all("Forms Style", pluck="style_name", order_by="style_name asc")


@frappe.whitelist()
def set_my_forms_style(style_name: str = ""):
	"""Every user's own personal style pick, independent of the System
	Default (styles.py's set_default_style(), System Manager only).
	style_name="" (or omitted) clears it, falling back to whatever the
	System Default currently is.
	"""
	ensure_logged_in()
	if style_name and not frappe.db.exists("Forms Style", style_name):
		frappe.throw(_("{0} is not a valid style.").format(style_name))
	frappe.defaults.set_user_default("forms_style", style_name or "")
	return {"my_style": style_name or None}


def get_company_bank_accounts(company: str | None = None):
	"""Enabled company (not personal) Bank Accounts, for pages that disburse or
	receive funds (Express Payroll, Express Purchase) and need the admin to pick
	which one to move money through. company is optional so Express Payroll
	(which resolves company from the selected Employee, not the nav selector,
	and loads this list before an employee is even picked) can still load the
	full cross-company list up front and rely on its own submit-time company
	match check instead.
	"""
	filters = {"is_company_account": 1, "disabled": 0}
	if company:
		filters["company"] = company
	return frappe.get_all(
		"Bank Account",
		filters=filters,
		fields=["name", "account", "company"],
		order_by="name asc",
	)


@frappe.whitelist()
def get_party_default(party_type: str, party: str, company: str):
	"""The remembered Item/Rate/Mode of Payment/Reference No/Remarks for this
	party (Customer or Supplier) on this company, if any — Express Sales/
	Purchase prefill their form with this the moment the party is picked. See
	upsert_party_default() for how these get written; also editable directly
	on /forms/user/binding.
	"""
	if not (party_type and party and company):
		return None
	row = frappe.db.get_value(
		"Forms Party Default",
		{"party_type": party_type, "party": party, "company": company},
		["item", "rate", "mode_of_payment", "reference_no", "remarks"],
		as_dict=True,
	)
	return row


@frappe.whitelist()
def get_print_format_settings(company: str | None = None):
	"""This company's configured default Print Formats for Express Sales/
	Purchase/Payroll's Invoice/Receipt/Check links (see
	/forms/user/printersettings), as a plain dict — {} if the company has no
	Forms Print Settings row yet (a company that's never visited the settings
	page), never None, so callers can always do settings.get(field) without a
	null check. Falls back to get_selected_company() when no company is
	passed, matching every other per-company lookup in this app. Each of the
	6 fields is None (rather than omitted) when left blank in the settings
	doc — callers are expected to substitute their own hardcoded default in
	that case, same fallback-if-blank convention documented on each field in
	the doctype itself.
	"""
	company = company or get_selected_company()
	if not company:
		return {}

	row = frappe.db.get_value(
		"Forms Print Settings",
		company,
		[
			"sales_invoice_format",
			"sales_receipt_format",
			"purchase_invoice_format",
			"purchase_receipt_format",
			"purchase_check_format",
			"payroll_check_format",
		],
		as_dict=True,
	)
	return row or {}


def upsert_party_default(
	party_type: str,
	party: str,
	company: str,
	item: str,
	rate=None,
	mode_of_payment: str | None = None,
	reference_no: str | None = None,
	remarks: str | None = None,
):
	"""Remember (or update) what a party's last Express Sale/Purchase used, so
	the next one prefills. Called from process_express_sale/
	process_express_purchase right after a successful submit — one write path
	whether the binding comes from a live transaction or a manual edit on
	/forms/user/binding (create_party_default() there calls this too).

	reference_no/remarks should be whatever the admin actually typed (or None
	when they left it blank) — never the auto-generated fallback either page
	substitutes on submit (a stale invoice number/auto-summary would be a
	wrong thing to prefill next time), so callers must pass the raw input,
	not whatever ended up on the Payment Entry.
	"""
	# Merges and renames can legitimately leave more than one binding pointing
	# at the surviving party.  Keep the newest one and remove the stale rows
	# before saving; otherwise the doctype's duplicate guard makes the sale
	# fail *after* its invoice and payment have already been prepared.
	existing = frappe.get_all(
		"Forms Party Default",
		filters={"party_type": party_type, "party": party, "company": company},
		fields=["name"],
		order_by="modified desc, creation desc",
	)
	name = existing[0].name if existing else None
	if len(existing) > 1:
		frappe.db.delete(
			"Forms Party Default",
			{"name": ["in", [row.name for row in existing[1:]]]},
		)
	doc = frappe.get_doc("Forms Party Default", name) if name else frappe.new_doc("Forms Party Default")
	doc.company = company
	doc.party_type = party_type
	doc.party = party
	doc.item = item
	doc.rate = rate
	doc.mode_of_payment = mode_of_payment or None
	doc.reference_no = reference_no or None
	doc.remarks = remarks or None
	doc.save(ignore_permissions=True)
	return doc


def get_companies():
	"""Companies this user may use on the forms portal — for the nav's company
	selector. Every Company, unless the user has ERPNext User Permission
	records for Company, in which case only those (that still exist). `abbr` is
	included: it's what the /c/<abbr>/forms/... URL prefix uses (see
	forms_company_abbr / the website_route_rules in hooks.py)."""
	permitted = permitted_docs("Company")
	filters = {"name": ["in", permitted]} if permitted is not None else None
	return frappe.get_all("Company", filters=filters, fields=["name", "abbr"], order_by="name asc")


# --- Per-URL company selection (/c/<abbr>/forms/user/<page>) -----------------
#
# The forms portal supports multiple companies. Historically the "current"
# company was a single per-user server-side default (get_selected_company),
# which meant one browser could only ever look at one company at a time. The
# /c/<abbr>/ URL prefix makes the company part of the address instead, so
# different tabs can hold different companies at once. Page GETs carry the
# abbr in the path (a website_route_rule rewrites it away before the page
# module runs, but frappe.local.request.path still has it); the portal's
# AJAX calls carry it in an X-Forms-Company header (added by forms_portal.js).
# A bare /forms/... URL with no prefix falls back to the per-user default,
# exactly as before.


def resolve_company_abbr(abbr: str | None) -> str | None:
	"""A Company name for a URL abbr segment (case-insensitive on `abbr`, then
	an exact Company-name fallback). None for an unknown value."""
	if not abbr:
		return None
	name = frappe.db.get_value("Company", {"abbr": abbr}, "name")
	if not name and frappe.db.exists("Company", abbr):
		name = abbr
	return name


def request_forms_company() -> str | None:
	"""The Company this request explicitly asked for via the /c/<abbr>/ URL
	prefix or the X-Forms-Company header — validated against the user's Company
	User Permissions (an abbr the user may not use is ignored, so a hand-edited
	URL can't widen access). None when the request carries no company hint.
	Cached on frappe.local for the life of the request."""
	if getattr(frappe.local, "_forms_req_company_done", False):
		return frappe.local._forms_req_company

	abbr = None
	req = getattr(frappe.local, "request", None)
	if req is not None:
		try:
			abbr = req.headers.get("X-Forms-Company")
		except Exception:
			abbr = None
		if not abbr:
			m = re.match(r"^/c/([^/]+)(?:/forms/|/?$)", req.path or "")
			if m:
				abbr = m.group(1)

	company = resolve_company_abbr(abbr) if abbr else None
	if company:
		permitted = permitted_docs("Company")
		if permitted is not None and company not in permitted:
			company = None

	frappe.local._forms_req_company = company
	frappe.local._forms_req_company_done = True
	return company


def forms_company_abbr(company: str | None = None) -> str | None:
	"""The abbr for the /c/<abbr>/ URL prefix — for `company` or, by default,
	the company currently in effect."""
	company = company or get_selected_company()
	if not company:
		return None
	return frappe.get_cached_value("Company", company, "abbr") or None


def get_selected_company():
	"""The company currently in effect for this user's session on the forms
	portal — set via the nav's company selector (or the same control on
	/forms/user/me), persisted through Frappe's own per-user default
	mechanism (the same one ERPNext's Desk company switcher uses under the
	hood), so it naturally survives across page loads and logins without this
	app needing its own session/cookie handling. Falls back to the site's
	global default company for users who've never touched the selector
	(single-company sites keep working exactly as before).

	Re-checked against real Company records on every call — a user default
	surviving from before a Company was renamed/deleted would otherwise keep
	being returned as-is (frappe.defaults doesn't validate on write, let
	alone on every later read), silently breaking every page that trusts this
	as a real Company name. Self-heals by clearing the stale default so this
	falls through to the global default from here on, rather than repeating
	a doomed lookup against the same bad value on every subsequent call.

	A user restricted to one or more Companies via User Permission never gets
	a company outside that set back from here — not their stale personal
	default, not the site global default — so every page that scopes itself
	by this value is automatically limited to what the user may see.

	When the request carries an explicit company (the /c/<abbr>/ URL prefix
	or the X-Forms-Company header — see request_forms_company()), that wins
	over the per-user default, so different browser tabs can each hold a
	different company at the same time. It's already permission-checked there.
	"""
	req_company = request_forms_company()
	if req_company:
		return req_company

	company = frappe.defaults.get_user_default("company")
	if company and not frappe.db.exists("Company", company):
		frappe.defaults.clear_user_default("company")
		company = None

	permitted = permitted_docs("Company")
	if permitted is not None:
		permitted_here = [c for c in permitted if frappe.db.exists("Company", c)]
		if company in permitted_here:
			return company
		return permitted_here[0] if permitted_here else None

	return company or frappe.defaults.get_global_default("company")


def get_dashboard_companies() -> list[str]:
	"""For /forms/user/dashboard's own site-wide numbers specifically --
	every *other* page in this app is scoped to get_selected_company()'s
	single company (the nav's own company selector, falling back to the
	user's personal default), by design. The Dashboard is different: a bare
	/forms/user/dashboard hit (no /c/<abbr>/ URL prefix, no
	X-Forms-Company header) is meant to read as the whole organization's
	activity, not whichever company happens to be that user's personal
	default at the moment -- see [[forms-dashboard-activity]] for the
	report that prompted this. Visiting /c/<abbr>/forms/user/dashboard (an
	explicit, deliberate choice) still narrows to that one company, exactly
	like every other page's own /c/<abbr>/ support already does.

	Returns every Company the user is permitted to see when there's no
	explicit company in the request, so a User-Permission-restricted user
	still only ever gets their own permitted companies aggregated -- never
	silently widened to the whole site just because they hit the bare URL.
	"""
	explicit = request_forms_company()
	if explicit:
		return [explicit]

	permitted = permitted_docs("Company")
	if permitted is not None:
		return [c for c in permitted if frappe.db.exists("Company", c)]

	return frappe.get_all("Company", pluck="name", ignore_permissions=True)


@frappe.whitelist()
def set_selected_company(company: str):
	ensure_logged_in()
	if not frappe.db.exists("Company", company):
		frappe.throw(_("{0} is not a valid company.").format(company))
	permitted = permitted_docs("Company")
	if permitted is not None and company not in permitted:
		frappe.throw(_("You are not permitted to use {0}.").format(company), frappe.PermissionError)
	frappe.defaults.set_user_default("company", company)
	return {"company": company}


def get_date_range_from_request() -> tuple[str | None, str | None]:
	"""Parse optional `from_date`/`to_date` query params (YYYY-MM-DD) off the request.

	Invalid values are silently dropped rather than raising — this only feeds
	an optional report filter, not a form submission that needs to error loudly.
	"""
	from frappe.utils import getdate

	def _parse(value):
		if not value:
			return None
		try:
			return str(getdate(value))
		except Exception:
			return None

	return _parse(frappe.form_dict.get("from_date")), _parse(frappe.form_dict.get("to_date"))


def get_month_range(months_ago: int = 0) -> tuple[str, str]:
	"""First/last calendar day of the month `months_ago` months before this one.

	months_ago=0 -> this month, months_ago=1 -> last month.
	"""
	from frappe.utils import add_months, get_first_day, get_last_day, nowdate

	anchor = add_months(nowdate(), -months_ago)
	return str(get_first_day(anchor)), str(get_last_day(anchor))


def get_date_range_with_default(default_months_ago: int = 1) -> tuple[str, str]:
	"""from_date/to_date off the request, defaulting to last month when neither is given."""
	from_date, to_date = get_date_range_from_request()
	if not from_date and not to_date:
		return get_month_range(default_months_ago)
	return from_date, to_date


def get_quick_ranges(from_date: str | None, to_date: str | None) -> dict:
	"""This-month/last-month ranges plus which one (if any) matches the current
	from_date/to_date, for quick-select buttons on GL report pages.
	"""
	this_month = get_month_range(0)
	last_month = get_month_range(1)

	if (from_date, to_date) == this_month:
		active = "this_month"
	elif (from_date, to_date) == last_month:
		active = "last_month"
	else:
		active = "custom"

	return {"this_month": this_month, "last_month": last_month, "active": active}


SALARY_SLIP_DATE_FIELDS = {"posting_date", "end_date"}


def get_salary_slip_component_breakdown(
	components: list[str],
	company: str,
	from_date: str | None = None,
	to_date: str | None = None,
	date_field: str = "posting_date",
) -> list[dict]:
	"""One row per submitted Salary Slip (not merged across slips) for the
	FICA/Treasurer Employee Breakdown tables — grouping by employee alone
	can't attach a single Start/End Date, Days Worked or Rate to a row when an
	employee has more than one slip in range. Grouping by `ss.name` instead
	keeps each slip's own period and Payment Days intact; an employee with two
	slips in range gets two rows.

	company is required (not optional) — on a multi-company site, Salary Slips
	from every company would otherwise get summed together into one report.
	"""
	if date_field not in SALARY_SLIP_DATE_FIELDS:
		frappe.throw(_("Invalid date_field: {0}").format(date_field))

	conditions = "ss.docstatus = 1 AND ss.company = %(company)s AND sd.salary_component IN %(components)s"
	values = {"company": company, "components": tuple(components)}

	if from_date:
		conditions += f" AND ss.{date_field} >= %(from_date)s"
		values["from_date"] = from_date
	if to_date:
		conditions += f" AND ss.{date_field} <= %(to_date)s"
		values["to_date"] = to_date

	return frappe.db.sql(
		f"""
		SELECT
			ss.name AS salary_slip,
			ss.employee,
			ss.employee_name,
			ss.start_date,
			ss.end_date,
			ss.payment_days,
			ss.gross_pay,
			sd.salary_component,
			SUM(sd.amount) AS amount
		FROM `tabSalary Detail` sd
		INNER JOIN `tabSalary Slip` ss ON ss.name = sd.parent
		WHERE {conditions}
		GROUP BY ss.name, sd.salary_component
		ORDER BY ss.employee_name, ss.start_date
		""",
		values,
		as_dict=True,
	)


@frappe.whitelist()
def quick_search(query: str = ""):
	"""Record search for the Quick Jump palette (Ctrl/Cmd+K, forms_quick_jump.js)
	— the "Pages" half of that palette is a closed, already permission-gated
	list rendered server-side once per page load (see
	ez_accounting.website_context._quick_jump_pages), so this only ever needs to
	cover records: customers, suppliers, employees, invoices, deposit sheets.

	Available to any logged-in portal user (not admin-only) — same as the
	pages themselves being reachable per-user already; permission scoping
	happens per-doctype below (apply_permitted_filter), same as every list
	endpoint in this app. Only searches when this site actually has a
	working ERPNext company behind it (GL_LINKS_SITES) — same gate every
	other ERPNext-dependent feature in this app already uses; imported
	locally to avoid a circular import (website_context.py imports from this
	module already).
	"""
	ensure_logged_in()

	query = (query or "").strip()
	if len(query) < 2:
		return {"records": []}

	from ez_accounting.website_context import GL_LINKS_SITES

	if frappe.local.site not in GL_LINKS_SITES:
		return {"records": []}

	company = get_selected_company()
	if not company:
		return {"records": []}

	like = f"%{query}%"
	records = []

	# Customer/Supplier: shared across every company on this bench (see
	# [[forms-company-sharing-removal]] — "Allowed To Transact With" was
	# removed outright), so no company filter here, only User Permission.
	customers = frappe.get_all(
		"Customer",
		filters=apply_permitted_filter({"disabled": 0}, "Customer"),
		or_filters=[["name", "like", like], ["customer_name", "like", like]],
		fields=["name", "customer_name"],
		order_by="customer_name asc",
		limit_page_length=5,
	)
	for c in customers:
		records.append(
			{
				"type": _("Customer"),
				"label": c.customer_name or c.name,
				"sub": c.name,
				"url": f"/desk/customer/{frappe.utils.quote(c.name)}",
			}
		)

	suppliers = frappe.get_all(
		"Supplier",
		filters=apply_permitted_filter({"disabled": 0}, "Supplier"),
		or_filters=[["name", "like", like], ["supplier_name", "like", like]],
		fields=["name", "supplier_name"],
		order_by="supplier_name asc",
		limit_page_length=5,
	)
	for s in suppliers:
		records.append(
			{
				"type": _("Supplier"),
				"label": s.supplier_name or s.name,
				"sub": s.name,
				"url": f"/desk/supplier/{frappe.utils.quote(s.name)}",
			}
		)

	employees = frappe.get_all(
		"Employee",
		filters=apply_permitted_filter({"company": company}, "Employee"),
		or_filters=[["name", "like", like], ["employee_name", "like", like]],
		fields=["name", "employee_name"],
		order_by="employee_name asc",
		limit_page_length=5,
	)
	for e in employees:
		records.append(
			{
				"type": _("Employee"),
				"label": e.employee_name or e.name,
				"sub": e.name,
				"url": f"/desk/employee/{frappe.utils.quote(e.name)}",
			}
		)

	# Sales/Purchase Invoice, Forms Bank Deposit Sheet: matched by document
	# number only (party name search already covered by Customer/Supplier
	# above) — submitted or cancelled, same "drafts don't belong here"
	# convention as every history table in this app.
	for doctype, doctype_label, url_slug in (
		("Sales Invoice", _("Sales Invoice"), "sales-invoice"),
		("Purchase Invoice", _("Purchase Invoice"), "purchase-invoice"),
		("Forms Bank Deposit Sheet", _("Deposit Sheet"), "forms-bank-deposit-sheet"),
	):
		rows = frappe.get_all(
			doctype,
			filters={"company": company, "docstatus": ["in", [1, 2]], "name": ["like", like]},
			fields=["name"],
			order_by="modified desc",
			limit_page_length=5,
		)
		for r in rows:
			records.append(
				{
					"type": doctype_label,
					"label": r.name,
					"sub": "",
					"url": f"/desk/{url_slug}/{frappe.utils.quote(r.name)}",
				}
			)

	return {"records": records[:20]}


def assign_payment_sequence_number(doc, method=None):
	"""doc_events hook (Payment Entry, on_submit): give the entry a
	permanent, per-company receipt/voucher number the first time it's ever
	submitted — <abbr>-00001, <abbr>-00002, ... — using Frappe's own atomic
	Series counter (the same mechanism naming series use under the hood, see
	frappe.model.naming.getseries), keyed by company so EIU/EMHS/etc. each
	count from 1 independently. Fires for every Payment Entry regardless of
	which page created it (Express Sales/Purchase, Payroll's Print Check,
	a plain Desk entry, ...) since it's a doc_event, not something each of
	those call sites has to remember to do itself.

	Assigned once, never reassigned or renumbered later — including if the
	entry is subsequently cancelled (see /forms/user/payments, which shows
	the number regardless of status, same as a check register keeping a
	voided check's own number rather than reusing it). An amended entry
	(cancel + resubmit as a new "<original>-1" document) is a genuinely new
	document, so it gets its own new number on its own submit, exactly as
	the original invoice/purchase number scheme does elsewhere in this app.

	The resulting order is effectively by `creation` (getseries() just hands
	out the next atomic number in call order, and on_submit fires once per
	real submission, which for this app's normal flows happens immediately
	after creation) — deliberately not `posting_date`, the transaction's own
	often-backdated date. The one-time backfill onto pre-existing entries
	(ez_accounting.patches.v1_0.backfill_payment_entry_sequence_numbers) makes this
	explicit by walking `creation ASC` directly.
	"""
	if doc.get("custom_sequence_number"):
		return

	abbr = frappe.get_cached_value("Company", doc.company, "abbr") if doc.company else None
	if not abbr:
		return

	from frappe.model.naming import getseries

	number = getseries(f"FORMS-PAYMENT-{abbr}-", 5)
	doc.db_set("custom_sequence_number", f"{abbr}-{number}", update_modified=False)


def assign_deposit_sequence_number(doc, method=None):
	"""doc_events hook (Forms Bank Deposit Sheet, on_submit) — same job as
	assign_payment_sequence_number() above, one series per doctype so a
	deposit sheet's numbering never collides with (or gets mixed up with) a
	plain Payment Entry's: <abbr>-DP-00001, <abbr>-DP-00002, ... See
	/forms/user/deposits and [[forms-payments-sequence]].
	"""
	if doc.get("custom_sequence_number"):
		return

	abbr = frappe.get_cached_value("Company", doc.company, "abbr") if doc.company else None
	if not abbr:
		return

	from frappe.model.naming import getseries

	number = getseries(f"FORMS-DEPOSIT-{abbr}-", 5)
	doc.db_set("custom_sequence_number", f"{abbr}-DP-{number}", update_modified=False)
