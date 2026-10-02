// mini Martin -- floating AI assistant chat button, site-wide on /forms/*.
// Mirrors forms_quick_jump.js's own conventions (self-guarding IIFE, plain
// fetch + X-Frappe-CSRF-Token, no frappe.call): a pure client-side widget
// with its own lazily-built DOM, so it shows up on every /forms/user/* and
// /forms/admin/* page without editing forms_nav.html or
// forms_admin_header.html separately. Backend: ez_accounting.api.mini_martin_chat.
(function () {
	function call(method, args) {
		return fetch("/api/method/" + method, {
			method: "POST",
			headers: {
				"Content-Type": "application/json",
				"X-Frappe-CSRF-Token": window.frappe ? frappe.csrf_token : ""
			},
			body: JSON.stringify(args || {})
		}).then(function (response) {
			return response.json().catch(function () { return {}; }).then(function (data) {
				if (!response.ok) {
					var error = new Error(extract_error_message(data));
					error.data = data;
					throw error;
				}
				return data;
			});
		});
	}

	// Frappe puts a frappe.throw()'d message in `_server_messages` (a
	// JSON-encoded array of JSON-encoded {message, ...} objects) -- neither
	// `_error_message` (not a real Frappe field) nor `exception` (usually the
	// raw traceback, or absent for a plain throw) carry it, so without this
	// every backend error -- a timeout, a disabled provider, a bad model,
	// anything -- collapsed into a useless generic "Request failed".
	function extract_error_message(data) {
		if (data && data._server_messages) {
			try {
				var messages = JSON.parse(data._server_messages).map(function (m) {
					try { return JSON.parse(m).message; } catch (e) { return m; }
				}).filter(Boolean);
				if (messages.length) return messages.join(" ");
			} catch (e) { /* fall through */ }
		}
		if (data && data.exception) return String(data.exception).split("\n").pop();
		return "Request failed";
	}

	function escape_html(s) {
		if (window.frappe && frappe.utils && frappe.utils.escape_html) return frappe.utils.escape_html(s);
		var div = document.createElement("div");
		div.textContent = s == null ? "" : String(s);
		return div.innerHTML;
	}

	// Minimal, XSS-safe markdown -> HTML (input is escaped first, then formatted).
	function render_markdown(src) {
		var codes = [];
		var text = escape_html(src == null ? "" : String(src)).replace(/\r\n?/g, "\n");
		text = text.replace(/```[^\n]*\n([\s\S]*?)```/g, function (m, code) {
			var lang = (m.match(/^```([\w+#-]*)/) || [])[1] || "";
			codes.push('<div class="mm-codebox"><div class="mm-codebar"><span class="mm-lang">' + (lang || "code") + '</span><button type="button" class="mm-copy" data-copy="code">Copy</button></div>' +
				'<pre class="mm-code"><code' + (lang ? ' class="language-' + lang + '"' : '') + '>' + code.replace(/\n$/, "") + '</code></pre></div>');
			return "\u0000" + (codes.length - 1) + "\u0000";
		});
		function inline(t) {
			t = t.replace(/`([^`\n]+)`/g, '<code class="mm-inline">$1</code>');
			t = t.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+|\/[^\s)]*)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
			t = t.replace(/\*\*([^*\n]+)\*\*|__([^_\n]+)__/g, function (m, a, b) { return "<strong>" + (a || b) + "</strong>"; });
			t = t.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
			t = t.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
			return t;
		}
		var lines = text.split("\n"), out = [], i = 0, m;
		function cells(l) { return l.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map(function (c) { c = c.trim(); return { html: inline(c), num: /^[-+($]*[\d,.]+%?\)?$/.test(c) }; }); }
		while (i < lines.length) {
			var line = lines[i];
			if (!line.trim()) { i++; continue; }
			if (/^\u0000\d+\u0000$/.test(line.trim())) { out.push(line.trim()); i++; continue; }
			if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { var lv = Math.min(m[1].length + 2, 6); out.push("<h" + lv + ' class="mm-h">' + inline(m[2]) + "</h" + lv + ">"); i++; continue; }
			if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
			if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
				var head = cells(line); i += 2; var body = [];
				while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { body.push(cells(lines[i])); i++; }
				out.push('<div class="mm-table-wrap"><table class="mm-table"><thead><tr>' + head.map(function (c) { return "<th" + (c.num ? ' class="mm-num"' : "") + ">" + c.html + "</th>"; }).join("") + "</tr></thead><tbody>" +
					body.map(function (r) { return "<tr>" + r.map(function (c) { return "<td" + (c.num ? ' class="mm-num"' : "") + ">" + c.html + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table></div>");
				continue;
			}
			if (/^&gt;\s?/.test(line)) {
				var q = [];
				while (i < lines.length && /^&gt;\s?/.test(lines[i])) { q.push(inline(lines[i].replace(/^&gt;\s?/, ""))); i++; }
				out.push("<blockquote>" + q.join("<br>") + "</blockquote>"); continue;
			}
			if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
				var ordered = /^\s*\d+[.)]\s+/.test(line), items = [];
				while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) { items.push("<li>" + inline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "")) + "</li>"); i++; }
				out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>")); continue;
			}
			var para = [];
			while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|&gt;|\s*([-*+]|\d+[.)])\s+|\s*\|.*\|\s*$|\u0000\d+\u0000)/.test(lines[i])) { para.push(inline(lines[i])); i++; }
			if (!para.length) { para.push(inline(lines[i])); i++; }
			out.push("<p>" + para.join("<br>") + "</p>");
		}
		return out.join("").replace(/\u0000(\d+)\u0000/g, function (m2, n) { return codes[+n]; });
	}

	var hljsLoading = null;
	function highlight_code(root) {
		var blocks = root.querySelectorAll("pre.mm-code code");
		if (!blocks.length) return;
		function run() { blocks.forEach(function (b) { try { window.hljs.highlightElement(b); } catch (e) {} }); }
		if (window.hljs) return run();
		if (!hljsLoading) {
			hljsLoading = new Promise(function (resolve, reject) {
				var sc = document.createElement("script");
				sc.src = "https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js";
				sc.onload = resolve; sc.onerror = reject;
				document.head.appendChild(sc);
			});
		}
		hljsLoading.then(run).catch(function () { hljsLoading = null; });
	}
	function copy_text(text, btn) {
		function done(ok) {
			var old = btn.getAttribute("data-label") || btn.textContent;
			btn.setAttribute("data-label", old);
			btn.textContent = ok ? "Copied ✓" : "Copy failed";
			setTimeout(function () { btn.textContent = old; }, 1400);
		}
		if (navigator.clipboard && window.isSecureContext) {
			navigator.clipboard.writeText(text).then(function () { done(true); }, function () { done(false); });
		} else {
			var ta = document.createElement("textarea");
			ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
			document.body.appendChild(ta); ta.select();
			var ok = false; try { ok = document.execCommand("copy"); } catch (e) {}
			ta.remove(); done(ok);
		}
	}
	var SUGGESTIONS = ["What can you do?", "Summarize this page", "Show unpaid invoices", "How do I record a payment?"];

	window.formsMarkdown = { render: render_markdown, highlight: highlight_code, copy: copy_text };

	var DEFAULT_AVATAR_URL = "/files/mini-martin.jpg";
	var DEFAULT_AGENT_NAME = "mini Martin";
	var DEFAULT_GREETING = "Hi, I'm mini Martin — ask me anything!";
	function size_storage_key() {
		return "forms_mini_martin_size:" + ((window.frappe && frappe.session && frappe.session.user) || "guest");
	}
	function load_stored_size() {
		try {
			var parsed = JSON.parse(localStorage.getItem(size_storage_key()) || "null");
			return parsed && Number.isFinite(parsed.width) && Number.isFinite(parsed.height) ? parsed : null;
		} catch (e) {
			return null;
		}
	}
	function save_stored_size(width, height) {
		try {
			localStorage.setItem(size_storage_key(), JSON.stringify({ width: width, height: height }));
		} catch (e) {
			// Resizing remains available when browser storage is unavailable.
		}
	}
	function position_storage_key() {
		return "forms_mini_martin_position:" + ((window.frappe && frappe.session && frappe.session.user) || "guest");
	}
	function load_stored_position() {
		try {
			var parsed = JSON.parse(localStorage.getItem(position_storage_key()) || "null");
			return parsed && Number.isFinite(parsed.left) && Number.isFinite(parsed.top) ? parsed : null;
		} catch (e) {
			return null;
		}
	}
	function save_stored_position(left, top) {
		try {
			localStorage.setItem(position_storage_key(), JSON.stringify({ left: left, top: top }));
		} catch (e) {
			// Dragging remains available when browser storage is unavailable.
		}
	}

	// Reply language -- a per-user preference (not an admin setting), so it's
	// stored client-side same as panel size/chat history, keyed per user so a
	// shared browser profile never mixes up two people's language choice.
	var DEFAULT_LANGUAGE = "Chinese (中文)";
	var LANGUAGES = [
		"Chinese (中文)", "English", "Spanish (Español)", "French (Français)", "German (Deutsch)",
		"Japanese (日本語)", "Korean (한국어)", "Portuguese (Português)", "Russian (Русский)",
		"Arabic (العربية)", "Hindi (हिन्दी)", "Italian (Italiano)", "Vietnamese (Tiếng Việt)",
		"Thai (ไทย)", "Indonesian (Bahasa Indonesia)", "Malay (Bahasa Melayu)", "Turkish (Türkçe)",
		"Dutch (Nederlands)", "Polish (Polski)", "Ukrainian (Українська)", "Greek (Ελληνικά)",
		"Hebrew (עברית)", "Swedish (Svenska)", "Filipino (Tagalog)", "Bengali (বাংলা)",
		"Persian (فارسی)", "Urdu (اردو)", "Czech (Čeština)", "Romanian (Română)", "Hungarian (Magyar)"
	];
	function lang_storage_key() {
		return "forms_mini_martin_lang:" + ((window.frappe && frappe.session && frappe.session.user) || "guest");
	}
	function load_stored_language() {
		try {
			var stored = localStorage.getItem(lang_storage_key());
			return stored && LANGUAGES.indexOf(stored) !== -1 ? stored : DEFAULT_LANGUAGE;
		} catch (e) {
			return DEFAULT_LANGUAGE;
		}
	}
	function save_stored_language(language) {
		try {
			localStorage.setItem(lang_storage_key(), language);
		} catch (e) {
			// Language selection remains usable for this session either way.
		}
	}
	frappe.ready(function () {
		if (window.location.pathname !== "/" && !window.location.pathname.match(/^\/(?:c\/[^/]+\/)?forms\//)) return;
		if (!frappe.session || frappe.session.user === "Guest") return;

		call("ez_accounting.api.get_mini_martin_config", {}).then(function (r) {
			var config = r.message || {};
			if (config.enabled === false) return;
			call("ez_accounting.api.get_mini_martin_history", { limit: 50 }).then(function (historyResponse) {
				build_widget({
					agentName: config.agent_name || DEFAULT_AGENT_NAME,
					avatarUrl: config.avatar || DEFAULT_AVATAR_URL,
					greeting: config.greeting_message || DEFAULT_GREETING,
					provider: config.provider || "AI CLI",
					subtitle: config.assistant_subtitle || "Virtual Martin · " + (config.provider || "AI CLI"),
					history: (historyResponse.message && historyResponse.message.messages) || []
				});
			}).catch(function () {
				build_widget({ agentName: config.agent_name || DEFAULT_AGENT_NAME, avatarUrl: config.avatar || DEFAULT_AVATAR_URL, greeting: config.greeting_message || DEFAULT_GREETING, provider: config.provider || "AI CLI", history: [] });
			});
		}).catch(function () {
			// Config fetch failed (e.g. a transient error) -- still show the
			// widget with sane defaults rather than silently disappearing.
			build_widget({ agentName: DEFAULT_AGENT_NAME, avatarUrl: DEFAULT_AVATAR_URL, greeting: DEFAULT_GREETING });
		});
	});

	function build_widget(config) {
		var agentName = config.agentName;
		var avatarUrl = config.avatarUrl;
		var greeting = config.greeting;
		var provider = config.provider || "AI CLI";
		var subtitle = config.subtitle || "Virtual Martin · " + provider;

		var history = [];
		var panel = null;
		var messagesEl = null;
		var pendingAttachment = null; // { attachment_id, file_name } once uploaded, shared with send()
		var replyLanguage = load_stored_language(); // shared with send()
		var sending = false;

		var trigger = document.createElement("button");
		trigger.type = "button";
		trigger.id = "mini-martin-trigger";
		trigger.className = "mini-martin-trigger";
		trigger.title = "Ask " + agentName;
		trigger.setAttribute("aria-label", "Ask " + agentName);
		trigger.innerHTML = '<img src="' + escape_html(avatarUrl) + '" alt="' + escape_html(agentName) + '" />' +
			'<span class="mini-martin-smile" aria-hidden="true"><svg viewBox="0 0 24 24" width="30" height="30" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round"><circle cx="8.5" cy="9.5" r="1" fill="#fff"/><circle cx="15.5" cy="9.5" r="1" fill="#fff"/><path d="M7 14c1.2 2.2 3 3.2 5 3.2s3.8-1 5-3.2"/></svg></span>';
		document.body.appendChild(trigger);

		function format_timestamp(value) {
			if (!value) return "";
			var parts = String(value).split(" ");
			try {
				return parts[1] ? parts[1].slice(0, 5) : "";
			} catch (e) { return String(value).slice(0, 16); }
		}

		function now_timestamp() {
			var d = new Date();
			return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
		}

		var lastDay = null;
		function local_day(d) { return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
		function day_label(day) {
			var today = new Date(), yest = new Date(Date.now() - 864e5);
			if (day === local_day(today)) return "Today";
			if (day === local_day(yest)) return "Yesterday";
			try { return frappe.datetime.str_to_user(day); } catch (e) { return day; }
		}
		function add_day_separator(rawTs) {
			var day = rawTs ? String(rawTs).split(" ")[0] : local_day(new Date());
			if (day === lastDay) return;
			lastDay = day;
			var sep = document.createElement("div");
			sep.className = "mini-martin-day";
			sep.innerHTML = "<span>" + escape_html(day_label(day)) + "</span>";
			messagesEl.appendChild(sep);
		}

		function add_message(role, text, timestamp, rawTs) {
			add_day_separator(rawTs);
			var bubble = document.createElement("div");
			bubble.className = "mini-martin-bubble mini-martin-bubble-" + role;
			var body_html = role === "assistant" ? render_markdown(text) : escape_html(text).replace(/\n/g, "<br>");
			bubble.innerHTML = '<div class="mini-martin-message-text' + (role === "assistant" ? ' mm-md' : '') + '">' + body_html + '</div>' +
				'<div class="mini-martin-message-time">' + escape_html(timestamp || now_timestamp()) + '</div>' +
				(role === "assistant" ? '<button type="button" class="mini-martin-copy-msg" title="Copy reply" aria-label="Copy reply">⧉</button>' : "");
			bubble.dataset.raw = text == null ? "" : String(text);
			messagesEl.appendChild(bubble);
			if (role === "assistant") highlight_code(bubble);
			messagesEl.scrollTop = messagesEl.scrollHeight;
			return bubble;
		}

		function set_typing(on, statusText) {
			var existing = messagesEl.querySelector(".mini-martin-typing");
			if (on && !existing) {
				var bubble = document.createElement("div");
				bubble.className = "mini-martin-bubble mini-martin-bubble-assistant mini-martin-typing";
				bubble.innerHTML = '<span></span><span></span><span></span><small class="mini-martin-progress"></small>';
				messagesEl.appendChild(bubble);
				messagesEl.scrollTop = messagesEl.scrollHeight;
			} else if (!on && existing) {
				existing.remove();
			}
			if (on) {
				existing = existing || messagesEl.querySelector(".mini-martin-typing");
				var progress = existing && existing.querySelector(".mini-martin-progress");
				if (progress) progress.textContent = statusText || "";
			}
		}

		function ensure_panel() {
			if (panel) return panel;
			panel = document.createElement("div");
			panel.className = "mini-martin-panel";
			panel.innerHTML =
				'<div class="mini-martin-resize-handle" role="separator" aria-label="Resize chat window" title="Drag to resize; double-click to reset" tabindex="0"></div>' +
				'<div class="mini-martin-header">' +
					'<div class="mini-martin-header-info">' +
						'<img class="mini-martin-avatar" src="' + escape_html(avatarUrl) + '" alt="' + escape_html(agentName) + '" />' +
						'<div><div class="mini-martin-title">' + escape_html(agentName) + '</div><div class="mini-martin-subtitle">' + escape_html(subtitle) + '</div></div>' +
					"</div>" +
					'<button type="button" class="mini-martin-max" aria-label="Maximize" title="Maximize">⤢</button>' +
					'<button type="button" class="mini-martin-close" aria-label="Close">&times;</button>' +
				"</div>" +
				'<div class="mini-martin-messages"></div>' +
				'<div class="mini-martin-attachment-chip" hidden></div>' +
				'<form class="mini-martin-input-row">' +
					'<input type="file" class="mini-martin-file-input" hidden />' +
					'<button type="button" class="mini-martin-plus-toggle" aria-label="Attach a file" title="Attach a text file">+</button>' +
					'<button type="button" class="mini-martin-emoji-toggle" aria-label="Add emoji" title="Add emoji">😊</button>' +
					'<div class="mini-martin-emoji-picker" hidden></div>' +
					'<button type="button" class="mini-martin-lang-toggle" aria-label="Reply language" title="Reply language">🌐</button>' +
					'<textarea class="mini-martin-input" rows="3" placeholder="Ask ' + escape_html(agentName) + '… (Enter to send, Shift+Enter for new line)" autocomplete="off"></textarea>' +
					'<button type="submit" class="mini-martin-send" aria-label="Send">➤</button>' +
				"</form>";
			document.body.appendChild(panel);
			apply_stored_size();
			apply_stored_position();
			setup_resize(panel.querySelector(".mini-martin-resize-handle"));
			setup_drag(panel.querySelector(".mini-martin-header"));

			messagesEl = panel.querySelector(".mini-martin-messages");
			var serverHistory = Array.isArray(config.history) ? config.history : [];
			if (serverHistory.length) {
				history = serverHistory;
				serverHistory.forEach(function (turn) {
					add_message(turn.role === "user" ? "user" : "assistant", turn.content, format_timestamp(turn.timestamp), turn.timestamp);
				});
			} else {
				add_message("assistant", greeting);
				history.push({ role: "assistant", content: greeting });
				var chips = document.createElement("div");
				chips.className = "mini-martin-chips";
				chips.innerHTML = SUGGESTIONS.map(function (q) { return '<button type="button" class="mini-martin-chip">' + escape_html(q) + "</button>"; }).join("");
				messagesEl.appendChild(chips);
			}

			panel.querySelector(".mini-martin-close").addEventListener("click", close_panel);
			panel.style.setProperty("--mm-avatar", "url('" + String(avatarUrl).replace(/['"()\\]/g, "") + "')");
			panel.querySelector(".mini-martin-max").addEventListener("click", function () {
				var on = panel.classList.toggle("is-max");
				this.textContent = on ? "⤡" : "⤢";
				this.title = on ? "Restore size" : "Maximize";
				if (panel.style.left || panel.style.top) set_panel_position(panel.offsetLeft, panel.offsetTop, !on);
			});
			messagesEl.addEventListener("click", function (e) {
				var chip = e.target.closest(".mini-martin-chip");
				if (chip) { if (!sending) send(chip.textContent); return; }
				var cb = e.target.closest(".mm-copy");
				if (cb) { var code = cb.closest(".mm-codebox").querySelector("code"); copy_text(code.textContent, cb); return; }
				var mb = e.target.closest(".mini-martin-copy-msg");
				if (mb) copy_text(mb.parentNode.dataset.raw || "", mb);
			});

			var langToggle = panel.querySelector(".mini-martin-lang-toggle");
			langToggle.title = "Reply language: " + replyLanguage;
			langToggle.addEventListener("click", function () {
				open_lang_modal();
			});

			var form = panel.querySelector(".mini-martin-input-row");
			var input = panel.querySelector(".mini-martin-input");
			var plusToggle = panel.querySelector(".mini-martin-plus-toggle");
			var fileInput = panel.querySelector(".mini-martin-file-input");
			var attachmentChip = panel.querySelector(".mini-martin-attachment-chip");

			function show_alert(message, danger) {
				if (window.frappe && frappe.show_alert) {
					frappe.show_alert({ message: message, indicator: danger ? "red" : "green" });
				}
			}

			function clear_attachment() {
				pendingAttachment = null;
				attachmentChip.hidden = true;
				attachmentChip.innerHTML = "";
				fileInput.value = "";
			}

			function render_attachment_chip(fileName, uploading) {
				attachmentChip.hidden = false;
				attachmentChip.innerHTML =
					'<span class="mini-martin-attachment-name">📎 ' + escape_html(fileName) + (uploading ? " (uploading…)" : "") + "</span>" +
					'<button type="button" class="mini-martin-attachment-remove" aria-label="Remove attachment">&times;</button>';
				attachmentChip.querySelector(".mini-martin-attachment-remove").addEventListener("click", clear_attachment);
			}

			plusToggle.addEventListener("click", function () {
				if (sending) return;
				fileInput.click();
			});
			fileInput.addEventListener("change", function () {
				upload_attachment(fileInput.files && fileInput.files[0]);
			});
			function upload_attachment(file) {
				if (!file) return;
				render_attachment_chip(file.name, true);
				var formData = new FormData();
				formData.append("file", file);
				fetch("/api/method/ez_accounting.api.upload_mini_martin_attachment", {
					method: "POST",
					headers: { "X-Frappe-CSRF-Token": window.frappe ? frappe.csrf_token : "" },
					body: formData
				}).then(function (response) {
					return response.json().catch(function () { return {}; }).then(function (data) {
						if (!response.ok) { var error = new Error(extract_error_message(data)); error.data = data; throw error; }
						return data;
					});
				}).then(function (data) {
					var result = data.message || {};
					pendingAttachment = { attachment_id: result.attachment_id, file_name: result.file_name || file.name };
					render_attachment_chip(pendingAttachment.file_name, false);
					input.focus();
				}).catch(function (error) {
					clear_attachment();
					show_alert(error.message || "Couldn't attach that file.", true);
				});
			}
			input.addEventListener("paste", function (e) {
				var items = (e.clipboardData && e.clipboardData.items) || [];
				for (var i = 0; i < items.length; i++) {
					if (items[i].kind === "file" && items[i].type.indexOf("image/") === 0) {
						var file = items[i].getAsFile();
						if (!file || sending) return;
						e.preventDefault();
						upload_attachment(file);
						return;
					}
				}
			});
			var emojiToggle = panel.querySelector(".mini-martin-emoji-toggle");
			var emojiPicker = panel.querySelector(".mini-martin-emoji-picker");
			var emojis = ["😀","😃","😄","😁","😊","😂","🤣","😍","🥰","😘","😎","🤔","🤗","😅","😢","😭","😮","😴","😇","🥳","👍","👎","👏","🙏","💪","👌","✌️","❤️","💛","💚","💙","💜","🔥","✨","🎉","✅","❌","⚠️","💡","📌","📎","📅","💰","📚","🏫","🙂"];
			emojiPicker.innerHTML = emojis.map(function (emoji) {
				return '<button type="button" class="mini-martin-emoji" data-emoji="' + emoji + '" aria-label="' + emoji + '">' + emoji + '</button>';
			}).join("");
			emojiToggle.addEventListener("click", function () {
				emojiPicker.hidden = !emojiPicker.hidden;
				if (!emojiPicker.hidden) input.focus();
			});
			emojiPicker.addEventListener("click", function (e) {
				var button = e.target.closest(".mini-martin-emoji");
				if (!button) return;
				var start = input.selectionStart == null ? input.value.length : input.selectionStart;
				var end = input.selectionEnd == null ? start : input.selectionEnd;
				input.value = input.value.slice(0, start) + button.dataset.emoji + input.value.slice(end);
				var cursor = start + button.dataset.emoji.length;
				input.setSelectionRange(cursor, cursor);
				input.dispatchEvent(new Event("input"));
				input.focus();
			});
			function resize_input() {
				input.style.height = "auto";
				input.style.height = input.scrollHeight + "px";
			}
			input.addEventListener("input", resize_input);
			input.addEventListener("keydown", function (e) {
				// Enter sends; Shift+Enter inserts a newline.
				if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
					e.preventDefault();
					form.requestSubmit();
				}
			});
			form.addEventListener("submit", function (e) {
				e.preventDefault();
				var text = input.value.trim();
				if ((!text && !pendingAttachment) || sending) return;
				input.value = "";
				emojiPicker.hidden = true;
				resize_input();
				send(text || ("Please look at the attached file: " + pendingAttachment.file_name));
			});

			return panel;
		}

		var langModal = null;
		function ensure_lang_modal() {
			if (langModal) return langModal;
			langModal = document.createElement("div");
			langModal.className = "mini-martin-lang-modal-overlay";
			langModal.hidden = true;
			langModal.innerHTML =
				'<div class="mini-martin-lang-modal">' +
					'<div class="mini-martin-lang-modal-header">' +
						"<h4>" + __("Reply language") + "</h4>" +
						'<button type="button" class="mini-martin-lang-modal-close" aria-label="Close">&times;</button>' +
					"</div>" +
					'<div class="mini-martin-lang-modal-list">' +
						LANGUAGES.map(function (lang) {
							return '<button type="button" class="mini-martin-lang-option" data-lang="' + escape_html(lang) + '">' + escape_html(lang) + "</button>";
						}).join("") +
					"</div>" +
				"</div>";
			document.body.appendChild(langModal);
			langModal.addEventListener("click", function (e) {
				if (e.target === langModal) close_lang_modal();
			});
			langModal.querySelector(".mini-martin-lang-modal-close").addEventListener("click", close_lang_modal);
			langModal.querySelector(".mini-martin-lang-modal-list").addEventListener("click", function (e) {
				var button = e.target.closest(".mini-martin-lang-option");
				if (!button) return;
				replyLanguage = button.getAttribute("data-lang");
				save_stored_language(replyLanguage);
				var toggle = panel && panel.querySelector(".mini-martin-lang-toggle");
				if (toggle) toggle.title = "Reply language: " + replyLanguage;
				update_lang_modal_active();
				close_lang_modal();
			});
			return langModal;
		}
		function update_lang_modal_active() {
			if (!langModal) return;
			Array.prototype.forEach.call(langModal.querySelectorAll(".mini-martin-lang-option"), function (btn) {
				btn.classList.toggle("is-active", btn.getAttribute("data-lang") === replyLanguage);
			});
		}
		function open_lang_modal() {
			ensure_lang_modal();
			update_lang_modal_active();
			langModal.hidden = false;
		}
		function close_lang_modal() {
			if (langModal) langModal.hidden = true;
		}

		function size_limits() {
			var availableWidth = Math.max(1, window.innerWidth - 32);
			var availableHeight = Math.max(1, window.innerHeight - 128);
			return {
				minWidth: Math.min(300, availableWidth),
				minHeight: Math.min(320, availableHeight),
				maxWidth: availableWidth,
				maxHeight: availableHeight
			};
		}

		function set_panel_size(width, height) {
			var limits = size_limits();
			width = Math.max(limits.minWidth, Math.min(width, limits.maxWidth));
			height = Math.max(limits.minHeight, Math.min(height, limits.maxHeight));
			panel.style.width = Math.round(width) + "px";
			panel.style.height = Math.round(height) + "px";
			return { width: Math.round(width), height: Math.round(height) };
		}

		function apply_stored_size() {
			var storedSize = load_stored_size();
			if (storedSize) set_panel_size(storedSize.width, storedSize.height);
		}

		function set_panel_position(left, top, persist) {
			var margin = 8;
			var maxLeft = Math.max(margin, window.innerWidth - panel.offsetWidth - margin);
			var maxTop = Math.max(margin, window.innerHeight - panel.offsetHeight - margin);
			left = Math.max(margin, Math.min(left, maxLeft));
			top = Math.max(margin, Math.min(top, maxTop));
			panel.style.left = Math.round(left) + "px";
			panel.style.top = Math.round(top) + "px";
			panel.style.right = "auto";
			panel.style.bottom = "auto";
			if (persist) save_stored_position(Math.round(left), Math.round(top));
		}

		function apply_stored_position() {
			var storedPosition = load_stored_position();
			if (storedPosition) set_panel_position(storedPosition.left, storedPosition.top, false);
		}

		function setup_drag(handle) {
			var start = null;
			handle.addEventListener("pointerdown", function (e) {
				if (e.target.closest("button") || (e.button !== 0 && e.pointerType !== "touch")) return;
				var rect = panel.getBoundingClientRect();
				start = { x: e.clientX, y: e.clientY, left: rect.left, top: rect.top };
				handle.setPointerCapture(e.pointerId);
				panel.classList.add("is-dragging");
				e.preventDefault();
			});
			handle.addEventListener("pointermove", function (e) {
				if (!start) return;
				set_panel_position(start.left + e.clientX - start.x, start.top + e.clientY - start.y, false);
			});
			function finish_drag() {
				if (!start) return;
				start = null;
				panel.classList.remove("is-dragging");
				save_stored_position(panel.offsetLeft, panel.offsetTop);
			}
			handle.addEventListener("pointerup", finish_drag);
			handle.addEventListener("pointercancel", finish_drag);
		}

		function setup_resize(handle) {
			var start = null;
			handle.addEventListener("pointerdown", function (e) {
				if (e.button !== 0 && e.pointerType !== "touch") return;
				var rect = panel.getBoundingClientRect();
				start = { x: e.clientX, y: e.clientY, left: rect.left, top: rect.top, width: panel.offsetWidth, height: panel.offsetHeight };
				handle.setPointerCapture(e.pointerId);
				panel.classList.add("is-resizing");
				e.preventDefault();
			});
			handle.addEventListener("pointermove", function (e) {
				if (!start) return;
				var size = set_panel_size(start.width + start.x - e.clientX, start.height + start.y - e.clientY);
				set_panel_position(start.left + start.width - size.width, start.top + start.height - size.height, false);
			});
			function finish_resize() {
				if (!start) return;
				start = null;
				panel.classList.remove("is-resizing");
				save_stored_size(panel.offsetWidth, panel.offsetHeight);
				save_stored_position(panel.offsetLeft, panel.offsetTop);
			}
			handle.addEventListener("pointerup", finish_resize);
			handle.addEventListener("pointercancel", finish_resize);
			handle.addEventListener("dblclick", function () {
				panel.style.width = "";
				panel.style.height = "";
				try { localStorage.removeItem(size_storage_key()); } catch (e) {}
			});
			handle.addEventListener("keydown", function (e) {
				var step = e.shiftKey ? 50 : 10;
				var width = panel.offsetWidth;
				var height = panel.offsetHeight;
				if (e.key === "ArrowLeft") width += step;
				else if (e.key === "ArrowRight") width -= step;
				else if (e.key === "ArrowUp") height += step;
				else if (e.key === "ArrowDown") height -= step;
				else return;
				var size = set_panel_size(width, height);
				save_stored_size(size.width, size.height);
				e.preventDefault();
			});
		}

		function open_panel() {
			ensure_panel();
			panel.classList.add("is-open");
			trigger.classList.add("is-active");
			setTimeout(function () {
				var input = panel.querySelector(".mini-martin-input");
				if (input) input.focus();
			}, 50);
		}

		function close_panel() {
			if (panel) panel.classList.remove("is-open");
			trigger.classList.remove("is-active");
		}

		function send(text) {
			var attachment = pendingAttachment;
			var displayText = attachment ? text + "\n📎 " + attachment.file_name : text;
			var oldChips = messagesEl.querySelector(".mini-martin-chips");
			if (oldChips) oldChips.remove();
			add_message("user", displayText);
			history.push({ role: "user", content: displayText });
			pendingAttachment = null;
			var chip = panel && panel.querySelector(".mini-martin-attachment-chip");
			if (chip) { chip.hidden = true; chip.innerHTML = ""; }
			var fileInputEl = panel && panel.querySelector(".mini-martin-file-input");
			if (fileInputEl) fileInputEl.value = "";
			sending = true;
			set_typing(true);
			call("ez_accounting.api.mini_martin_chat", {
				message: text,
				history: history.slice(0, -1).slice(-20),
				url: window.location.href,
				attachment_id: attachment ? attachment.attachment_id : "",
				reply_language: replyLanguage
			})
				.then(function (data) {
					var result = data.message || {};
					if (result.pending && result.task_id) {
						poll_task(result.task_id, 0);
						return;
					}
					finish_reply(result.reply || "…");
				})
				.catch(function (error) {
					set_typing(false);
					sending = false;
					add_message("assistant", "Sorry, I couldn't respond just now (" + (error.message || "error") + ").");
				});
		}

		function finish_reply(reply) {
			set_typing(false);
			sending = false;
			add_message("assistant", reply);
			history.push({ role: "assistant", content: reply });
		}

		function poll_task(taskId, failures) {
			setTimeout(function () {
				call("ez_accounting.api.get_mini_martin_task", { task_id: taskId }).then(function (data) {
					var result = data.message || {};
					if (result.status === "queued") {
						set_typing(true, "Queued…");
					} else if (result.status === "running") {
						var elapsed = result.started_at ? Math.max(0, Math.floor(Date.now() / 1000 - result.started_at)) : 0;
						set_typing(true, "Working… " + elapsed + "s / " + (result.timeout_seconds || "?") + "s");
					}
					if (result.status === "complete") {
						finish_reply(result.reply || "Done.");
					} else if (result.status === "error") {
						finish_reply("Sorry, I couldn't complete that request (" + (result.error || "error") + ").");
					} else {
						poll_task(taskId, 0);
					}
				}).catch(function () {
					// Python edits may briefly restart the web process. Keep polling
					// through that window instead of losing the completed Codex result.
					if (failures < 30) poll_task(taskId, failures + 1);
					else finish_reply("The code task is still running. Please reopen Mini Martin in a moment.");
				});
			}, 2000);
		}

		trigger.addEventListener("click", function () {
			if (panel && panel.classList.contains("is-open")) {
				close_panel();
			} else {
				open_panel();
			}
		});

		document.addEventListener("keydown", function (e) {
			if (e.key !== "Escape") return;
			if (langModal && !langModal.hidden) { close_lang_modal(); return; }
			if (panel && panel.classList.contains("is-open")) close_panel();
		});
		window.addEventListener("resize", function () {
			if (panel && (panel.style.width || panel.style.height)) {
				set_panel_size(panel.offsetWidth, panel.offsetHeight);
			}
			if (panel && (panel.style.left || panel.style.top)) {
				set_panel_position(panel.offsetLeft, panel.offsetTop, true);
			}
		});
	}
})();
