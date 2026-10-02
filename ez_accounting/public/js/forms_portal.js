// ===========================================================================
// Multi-company URL prefix (/c/<abbr>/forms/user/...) — keeps a browser tab
// pinned to one company regardless of the per-user server-side default, so
// different tabs can hold different companies at once.
//
//  - The company abbr comes from the URL path (/c/<abbr>/forms/...) or, on a
//    bare /forms/... URL, from window.FORMS_COMPANY_ABBR (the resolved default,
//    injected by forms_nav.html).
//  - Every portal API call (fetch + XHR — the pages' own call() helpers and
//    frappe.call alike) gets an X-Forms-Company header so the server scopes to
//    the same company the tab is showing (ez_accounting.api.request_forms_company).
//  - Every /forms/... link on the page (server-rendered and JS-built) is
//    rewritten to carry the /c/<abbr>/ prefix.
// ===========================================================================
(function () {
	if (typeof window === "undefined" || !window.location) return;
	var pathMatch = window.location.pathname.match(/^\/(?:c\/([^/]+)\/)?forms\//);
	var companyRootMatch = window.location.pathname.match(/^\/c\/([^/]+)\/?$/);
	if (!pathMatch && !companyRootMatch) return;

	var pathAbbr = (pathMatch && pathMatch[1]) || (companyRootMatch && companyRootMatch[1]) || null;

	function currentAbbr() {
		return pathAbbr || window.FORMS_COMPANY_ABBR || null;
	}
	function currentPrefix() {
		var a = currentAbbr();
		return a ? "/c/" + a : "";
	}
	window.FORMS_URL_PREFIX = currentPrefix();

	function isPortalApi(url) {
		if (typeof url !== "string") return false;
		return url.indexOf("/api/method/") !== -1;
	}

	// --- fetch (the pages' local call() helpers, forms_quick_jump.js) --------
	if (window.fetch) {
		var _fetch = window.fetch;
		window.fetch = function (input, init) {
			try {
				var abbr = currentAbbr();
				var url = typeof input === "string" ? input : (input && input.url) || "";
				if (abbr && isPortalApi(url)) {
					init = init || {};
					var h = new Headers(init.headers || (typeof input !== "string" && input.headers) || undefined);
					if (!h.has("X-Forms-Company")) h.set("X-Forms-Company", abbr);
					init.headers = h;
				}
			} catch (e) { /* fall through with the original args */ }
			return _fetch.call(this, input, init);
		};
	}

	// --- XMLHttpRequest (jQuery $.ajax -> frappe.call, any raw XHR) ----------
	var _open = XMLHttpRequest.prototype.open;
	XMLHttpRequest.prototype.open = function (method, url) {
		this.__formsApiUrl = url;
		return _open.apply(this, arguments);
	};
	var _send = XMLHttpRequest.prototype.send;
	XMLHttpRequest.prototype.send = function (body) {
		try {
			var abbr = currentAbbr();
			if (abbr && isPortalApi(this.__formsApiUrl)) {
				this.setRequestHeader("X-Forms-Company", abbr);
			}
		} catch (e) { /* header already sent / disallowed — ignore */ }
		return _send.apply(this, arguments);
	};

	// --- link rewriting ----------------------------------------------------
	function prefixHref(a) {
		var href = a.getAttribute("href");
		if (!href || href.charAt(0) !== "/") return;
		if (href.indexOf("/forms/") !== 0) return; // not a portal link (or already /c/-prefixed)
		var prefix = currentPrefix();
		if (!prefix) return;
		a.setAttribute("href", prefix + href);
	}

	function rewriteAll(root) {
		if (!currentPrefix()) return;
		var links = (root || document).querySelectorAll('a[href^="/forms/"]');
		for (var i = 0; i < links.length; i++) prefixHref(links[i]);
	}

	function onReady(fn) {
		if (document.readyState !== "loading") fn();
		else document.addEventListener("DOMContentLoaded", fn);
	}

	onReady(function () {
		rewriteAll(document);

		// JS-built links (history tables, dashboard widgets, search results) —
		// rewrite them just before they're followed. mousedown covers left and
		// middle click; contextmenu covers "open link in new tab" from the
		// right-click menu (the whole point of this feature).
		function handler(e) {
			var t = e.target;
			var a = t && (t.closest ? t.closest('a[href^="/forms/"]') : null);
			if (a) prefixHref(a);
		}
		document.addEventListener("mousedown", handler, true);
		document.addEventListener("contextmenu", handler, true);
		document.addEventListener("click", handler, true);

		// Catch links added to the DOM after load (without eagerly rewriting on
		// every mutation — just mark the subtree dirty and sweep on a microtask).
		if (window.MutationObserver && document.body) {
			var pending = false;
			new MutationObserver(function (muts) {
				if (pending) return;
				for (var i = 0; i < muts.length; i++) {
					if (muts[i].addedNodes && muts[i].addedNodes.length) {
						pending = true;
						Promise.resolve().then(function () { pending = false; rewriteAll(document); });
						break;
					}
				}
			}).observe(document.body, { childList: true, subtree: true });
		}
	});

	// For code that navigates by assignment rather than an <a> (quick jump).
	window.formsPortalUrl = function (url) {
		if (typeof url === "string" && url.indexOf("/forms/") === 0) return currentPrefix() + url;
		return url;
	};
})();

// Loaded on every website page (web_include_js in hooks.py), but only acts on
// the /forms/* portal. Turns the portal's app-wide inline-save convention —
// every auto-saving field flips a "save-status" element to
// class "save-status is-saved" with text "Saved" — into a toast, so every
// Apply DaisyUI's select component to every portal select, including controls
// created later by table/rendering code. Keeping this centralized means new
// pages inherit the app's select treatment automatically.
(function () {
	if (typeof window === "undefined" || !window.location) return;
	if (!window.location.pathname.match(/^\/(?:c\/[^/]+\/)?forms\//)) return;

	function enhance(root) {
		if (!root || root.nodeType !== 1) return;
		if (root.matches && root.matches("select")) root.classList.add("select", "select-neutral");
		if (!root.querySelectorAll) return;
		var selects = root.querySelectorAll("select");
		for (var i = 0; i < selects.length; i++) selects[i].classList.add("select", "select-neutral");
	}

	function start() {
		enhance(document.body);
		if (!window.MutationObserver || !document.body) return;
		new MutationObserver(function (mutations) {
			for (var i = 0; i < mutations.length; i++) {
				for (var j = 0; j < mutations[i].addedNodes.length; j++) enhance(mutations[i].addedNodes[j]);
			}
		}).observe(document.body, { childList: true, subtree: true });
	}

	if (document.readyState !== "loading") start();
	else document.addEventListener("DOMContentLoaded", start);
})();

// auto-save across every /forms/user/* page gives the same feedback without
// each page having to call frappe.show_alert() itself.
(function () {
	if (typeof window === "undefined" || !window.location) return;
	if (!window.location.pathname.match(/^\/(?:c\/[^/]+\/)?forms\//)) return;

	function onReady(fn) {
		if (document.readyState !== "loading") fn();
		else document.addEventListener("DOMContentLoaded", fn);
	}

	onReady(function () {
		if (!window.frappe || typeof frappe.show_alert !== "function" || !window.MutationObserver) return;
		if (!document.body) return;

		var translate = typeof window.__ === "function" ? window.__ : function (s) { return s; };
		var pending = 0;
		var timer = null;

		function flush() {
			var n = pending;
			pending = 0;
			timer = null;
			if (!n) return;
			frappe.show_alert({
				message: n > 1 ? translate("Saved") + " × " + n : translate("Saved"),
				indicator: "green"
			});
		}

		// One toast per burst — several fields (or a rapid re-save) collapse
		// into a single "Saved" (or "Saved x N").
		function schedule() {
			if (timer) clearTimeout(timer);
			timer = setTimeout(flush, 300);
		}

		var observer = new MutationObserver(function (mutations) {
			for (var i = 0; i < mutations.length; i++) {
				var m = mutations[i];
				if (m.type !== "attributes" || m.attributeName !== "class") continue;
				var el = m.target;
				if (!el.classList || !el.classList.contains("save-status") || !el.classList.contains("is-saved")) {
					continue;
				}
				// Only when it *just became* is-saved (not a later textContent-only
				// tweak, which doesn't touch class, or an already-saved element
				// whose class is rewritten to the same value).
				if (m.oldValue && m.oldValue.indexOf("is-saved") !== -1) continue;
				pending++;
				schedule();
			}
		});

		observer.observe(document.body, {
			subtree: true,
			attributes: true,
			attributeOldValue: true,
			attributeFilter: ["class"]
		});

		// A page that auto-saves then reloads (e.g. /forms/user/me's company /
		// style switch) can't show its own toast — it stashes a flag first and
		// we surface it here on the next load.
		try {
			if (window.sessionStorage && window.sessionStorage.getItem("forms_portal_saved_toast")) {
				window.sessionStorage.removeItem("forms_portal_saved_toast");
				frappe.show_alert({ message: translate("Saved"), indicator: "green" });
			}
		} catch (e) { /* sessionStorage blocked — ignore */ }
	});
})();

// ===========================================================================
// "Who's online" heartbeat — every logged-in viewer's own browser calls
// ez_accounting.api.ping() on an interval for as long as a /forms/* tab stays open,
// so the Dashboard's online count/list (ez_accounting.api.get_online_users()) is a
// real-time signal instead of one that only updates when tabSessions itself
// happens to get written (throttled to roughly once every ~10 minutes per
// session by Frappe's own Session.update() — far too coarse for "online
// now"). Wrapped in frappe.ready() (not a bare top-level IIFE) since this
// script loads in <head> via web_include_js, before frappe.session/
// frappe.csrf_token are necessarily ready yet.
frappe.ready(function () {
	if (!window.location.pathname.match(/^\/(?:c\/[^/]+\/)?forms\//)) return;
	if (!frappe.session || frappe.session.user === "Guest") return;

	var PING_INTERVAL_MS = 30000;

	function ping() {
		fetch("/api/method/ez_accounting.api.ping", {
			method: "POST",
			headers: { "Content-Type": "application/json", "X-Frappe-CSRF-Token": frappe.csrf_token },
			body: "{}"
		}).catch(function () { /* one missed beat is fine — the next interval tries again */ });
	}

	ping();
	setInterval(ping, PING_INTERVAL_MS);
});

// ===========================================================================
// Pull-to-refresh (Android style) — on touch screens, dragging down from the
// very top of any /forms/* page shows a spinner bubble; releasing past the
// threshold reloads the page. Ignored when the page is already scrolled, the
// touch starts inside a scrollable inner box, modal, or form field, so it
// never fights normal scrolling or dialogs.
(function () {
	if (!window.location.pathname.match(/^\/(?:c\/[^/]+\/)?forms\//)) return;
	if (!("ontouchstart" in window)) return;

	var THRESHOLD = 70, MAX_PULL = 110;
	var startY = null, pull = 0, armed = false, bubble = null;

	function scrolledInside(el) {
		while (el && el !== document.body && el !== document.documentElement) {
			if (el.matches && el.matches("input, textarea, select, [contenteditable], .modal, [role=dialog]")) return true;
			var oy = window.getComputedStyle(el).overflowY;
			if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight) return true;
			el = el.parentElement;
		}
		return false;
	}

	function ensureBubble() {
		if (bubble) return bubble;
		bubble = document.createElement("div");
		bubble.className = "forms-ptr-bubble";
		bubble.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-3-6.7"/><path d="M21 3v6h-6"/></svg>';
		document.body.appendChild(bubble);
		return bubble;
	}

	function setBubble(y, spinning) {
		var b = ensureBubble();
		b.style.transform = "translate(-50%, " + (y - 44) + "px) rotate(" + (spinning ? 0 : y * 4) + "deg)";
		b.style.opacity = Math.min(1, y / THRESHOLD);
		b.classList.toggle("is-spinning", !!spinning);
		b.classList.toggle("is-ready", y >= THRESHOLD);
	}

	function reset() {
		startY = null; pull = 0; armed = false;
		if (bubble) { bubble.style.opacity = 0; bubble.style.transform = "translate(-50%, -44px)"; bubble.classList.remove("is-spinning"); }
	}

	document.addEventListener("touchstart", function (e) {
		if (e.touches.length !== 1 || window.scrollY > 0 || scrolledInside(e.target)) { startY = null; return; }
		startY = e.touches[0].clientY; pull = 0; armed = false;
	}, { passive: true });

	document.addEventListener("touchmove", function (e) {
		if (startY === null) return;
		var dy = e.touches[0].clientY - startY;
		if (dy <= 0 || window.scrollY > 0) { if (armed) reset(); return; }
		armed = true;
		pull = Math.min(MAX_PULL, dy * 0.5);
		setBubble(pull, false);
	}, { passive: true });

	document.addEventListener("touchend", function () {
		if (startY === null) return;
		if (armed && pull >= THRESHOLD) {
			setBubble(THRESHOLD, true);
			window.location.reload();
		} else {
			reset();
		}
		startY = null;
	}, { passive: true });

	document.addEventListener("touchcancel", reset, { passive: true });
})();
