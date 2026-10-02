// Loaded on every website page (web_include_js in hooks.py), but only acts on
// the /forms/* portal — same self-guard as forms_portal.js. The global
// Ctrl/Cmd+K command palette: "Pages" (the permission-gated list the server
// already rendered into window.FORMS_QUICK_JUMP_PAGES, see
// forms.website_context._quick_jump_pages — filtered here client-side, no
// round trip) plus "Records" (customers/suppliers/employees/invoices/deposit
// sheets, via ez_accounting.api.quick_search — see that function's own docstring for
// why it only fires on GL_LINKS_SITES).
(function () {
	if (typeof window === "undefined" || !window.location) return;
	// An authenticated `/` request is the Forms dashboard via the app's
	// user-aware homepage hook. Its browser pathname stays `/`, so allow it
	// alongside explicit /forms/* routes; the trigger check below remains the
	// final guard and keeps this inert on the guest login page at `/`.
	if (window.location.pathname !== "/" && !window.location.pathname.match(/^\/(?:c\/[^/]+\/)?forms\//)) return;

	function onReady(fn) {
		if (document.readyState !== "loading") fn();
		else document.addEventListener("DOMContentLoaded", fn);
	}

	function translate(s, args) {
		if (typeof window.__ === "function") return window.__(s, args);
		return s;
	}

	function escape_html(s) {
		if (window.frappe && frappe.utils && typeof frappe.utils.escape_html === "function") {
			return frappe.utils.escape_html(s);
		}
		var div = document.createElement("div");
		div.textContent = s == null ? "" : String(s);
		return div.innerHTML;
	}

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
				if (!response.ok) throw new Error(data._error_message || data.exception || "Request failed");
				return data;
			});
		});
	}

	onReady(function () {
		var trigger = document.getElementById("forms-quick-jump-trigger");
		if (!trigger) return;

		var PAGES = Array.isArray(window.FORMS_QUICK_JUMP_PAGES) ? window.FORMS_QUICK_JUMP_PAGES : [];

		// Built lazily on first open — no DOM/CSS cost on a page load that
		// never uses this.
		var overlay = null;
		var input = null;
		var resultsBox = null;
		var items = []; // flat, currently-rendered {url, el} list, for arrow-key nav
		var activeIndex = -1;
		var searchDebounce = null;
		var searchToken = 0; // guards a slow response from overwriting a newer one

		function normalize(text) {
			return String(text || "").toLowerCase();
		}

		function filter_pages(query) {
			var q = normalize(query);
			if (!q) return PAGES;
			return PAGES.filter(function (p) { return normalize(p.label).indexOf(q) !== -1; });
		}

		function set_active(index) {
			items.forEach(function (it) { it.el.classList.remove("is-active"); });
			if (index >= 0 && items[index]) {
				items[index].el.classList.add("is-active");
				items[index].el.scrollIntoView({ block: "nearest" });
			}
			activeIndex = index;
		}

		function add_section(label, rows, type_label_fn) {
			if (!rows.length) return;
			var sectionLabel = document.createElement("div");
			sectionLabel.className = "quick-jump-section-label";
			sectionLabel.textContent = label;
			resultsBox.appendChild(sectionLabel);

			rows.forEach(function (row) {
				var el = document.createElement("div");
				el.className = "quick-jump-item";
				el.innerHTML =
					"<span>" + escape_html(row.label) +
						(row.sub ? '<span class="quick-jump-item-sub">' + escape_html(row.sub) + "</span>" : "") +
					"</span>" +
					'<span class="quick-jump-item-type">' + escape_html(type_label_fn(row)) + "</span>";
				el.addEventListener("mousedown", function (e) {
					e.preventDefault();
					navigate(row.url);
				});
				resultsBox.appendChild(el);
				items.push({ url: row.url, el: el });
			});
		}

		function render(pageRows, recordRows, recordsLoading) {
			resultsBox.innerHTML = "";
			items = [];
			activeIndex = -1;

			add_section(translate("Pages"), pageRows, function () { return translate("page"); });
			if (recordRows) {
				add_section(translate("Records"), recordRows, function (row) { return row.type; });
			} else if (recordsLoading) {
				var loading = document.createElement("div");
				loading.className = "quick-jump-empty";
				loading.textContent = translate("Searching…");
				resultsBox.appendChild(loading);
			}

			if (!items.length && !recordsLoading) {
				var empty = document.createElement("div");
				empty.className = "quick-jump-empty";
				empty.textContent = translate("No matches.");
				resultsBox.appendChild(empty);
			}
		}

		function run_search() {
			var query = input.value.trim();
			var pageRows = filter_pages(query);

			if (query.length < 2) {
				render(pageRows, null, false);
				return;
			}

			render(pageRows, null, true);
			var token = ++searchToken;
			call("ez_accounting.api.quick_search", { query: query }).then(function (r) {
				if (token !== searchToken) return; // a newer keystroke's response already landed
				render(filter_pages(input.value.trim()), (r.message && r.message.records) || [], false);
			}).catch(function () {
				if (token !== searchToken) return;
				render(pageRows, [], false);
			});
		}

		function navigate(url) {
			if (!url) return;
			// Keep the current company's /c/<abbr>/ prefix on portal links
			// (forms_portal.js sets up window.formsPortalUrl).
			if (typeof window.formsPortalUrl === "function") url = window.formsPortalUrl(url);
			window.location.href = url;
		}

		function ensure_overlay() {
			if (overlay) return;

			overlay = document.createElement("div");
			overlay.className = "quick-jump-overlay d-none";
			overlay.innerHTML =
				'<div class="quick-jump-panel" role="dialog" aria-modal="true" aria-label="' + escape_html(translate("Quick Jump")) + '">' +
					'<div class="quick-jump-input-row">' +
						'<i class="fa fa-search" aria-hidden="true"></i>' +
						'<input type="text" id="quick-jump-input" autocomplete="off" placeholder="' +
							escape_html(translate("Search pages, customers, invoices…")) + '">' +
						'<span class="quick-jump-esc-hint">Esc</span>' +
					"</div>" +
					'<div class="quick-jump-results" id="quick-jump-results"></div>' +
				"</div>";
			document.body.appendChild(overlay);

			input = overlay.querySelector("#quick-jump-input");
			resultsBox = overlay.querySelector("#quick-jump-results");

			input.addEventListener("input", function () {
				clearTimeout(searchDebounce);
				searchDebounce = setTimeout(run_search, 250);
			});

			input.addEventListener("keydown", function (e) {
				if (e.key === "ArrowDown") {
					e.preventDefault();
					set_active(Math.min(activeIndex + 1, items.length - 1));
				} else if (e.key === "ArrowUp") {
					e.preventDefault();
					set_active(Math.max(activeIndex - 1, 0));
				} else if (e.key === "Enter") {
					e.preventDefault();
					if (activeIndex >= 0 && items[activeIndex]) navigate(items[activeIndex].url);
					else if (items.length === 1) navigate(items[0].url);
				}
				// Escape is handled by the document-level listener below.
			});

			// Backdrop click closes — unlike the Pay/Credit-Note modals elsewhere
			// in this app, there's no form state here to lose to a stray click,
			// so the usual command-palette convention (click outside = dismiss)
			// applies rather than the "close button / Esc only" rule those use.
			overlay.addEventListener("mousedown", function (e) {
				if (e.target === overlay) close_palette();
			});
		}

		function open_palette() {
			ensure_overlay();
			overlay.classList.remove("d-none");
			input.value = "";
			run_search();
			// Focus after the overlay is actually visible, not before.
			window.requestAnimationFrame(function () { input.focus(); });
		}

		function close_palette() {
			if (overlay) overlay.classList.add("d-none");
		}

		function is_open() {
			return !!overlay && !overlay.classList.contains("d-none");
		}

		trigger.addEventListener("click", open_palette);

		document.addEventListener("keydown", function (e) {
			var isMod = e.ctrlKey || e.metaKey;
			if (isMod && (e.key === "k" || e.key === "K")) {
				e.preventDefault();
				if (is_open()) close_palette();
				else open_palette();
				return;
			}
			if (e.key === "Escape" && is_open()) close_palette();
		});
	});
})();
