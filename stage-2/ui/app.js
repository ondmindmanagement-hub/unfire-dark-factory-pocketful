"use strict";
/*
 * Browser client. No framework, no external assets.
 *
 * Reliability rules this file implements:
 *  - Every money-moving form owns a retry identity: one idempotency key per
 *    exact request body. Resubmitting an unchanged form replays the same key, so
 *    a double submit or a retry after a lost response moves money at most once;
 *    editing any field mints a new key.
 *  - A network failure or 5xx is an *uncertain* outcome, never shown as a
 *    refusal; the form stays retryable with the same key and body.
 *  - Every read is sequence-numbered: a response is applied only if no later
 *    read of the same resource has already been applied (latest refresh wins).
 */
(function () {
  const TOKEN_KEY = "wallet.session.token";
  const app = document.getElementById("app");

  // ------------------------------------------------------------------ utils
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "testid") el.setAttribute("data-testid", v);
      else if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  function newKey() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  }

  function stable(value) {
    if (Array.isArray(value)) return "[" + value.map(stable).join(",") + "]";
    if (value && typeof value === "object") {
      return "{" + Object.keys(value).sort().map((k) => JSON.stringify(k) + ":" + stable(value[k])).join(",") + "}";
    }
    return JSON.stringify(value);
  }

  /** One idempotency key per distinct request body. */
  class RetryIdentity {
    constructor() { this.fingerprint = null; this.key = null; }
    keyFor(scope, body) {
      const fp = scope + "|" + stable(body);
      if (fp !== this.fingerprint) { this.fingerprint = fp; this.key = newKey(); }
      return this.key;
    }
  }
  const identities = new Map();
  function identity(name) {
    if (!identities.has(name)) identities.set(name, new RetryIdentity());
    return identities.get(name);
  }

  function money(minor, mu, cur) {
    const s = String(minor);
    if (!mu) return s + " " + cur;
    const p = s.padStart(mu + 1, "0");
    return p.slice(0, -mu) + "." + p.slice(-mu) + " " + cur;
  }

  function plainDecimal(minor, mu) {
    return money(minor, mu, "").trim();
  }

  /** Parse a typed decimal into exact minor units, or return an error string. */
  function parseAmount(text, mu) {
    const t = String(text || "").trim();
    if (!t) return { error: "Enter an amount." };
    const m = /^(\d+)(?:\.(\d+))?$/.exec(t);
    if (!m) return { error: "Enter an amount using digits, like " + (mu ? "12." + "5".padEnd(mu, "0") : "1200") + "." };
    const frac = m[2] || "";
    if (frac.length > mu || (mu === 0 && m[2] !== undefined)) {
      return { error: mu === 0 ? "This currency has no decimal places." : "Use at most " + mu + " decimal places." };
    }
    const whole = m[1].replace(/^0+(?=\d)/, "");
    if (whole.length > 10) return { error: "That amount is too large." };
    const minor = Number(whole) * Math.pow(10, mu) + Number(frac.padEnd(mu, "0") || "0");
    if (minor < 1) return { error: "Enter an amount greater than zero." };
    if (minor > 1000000000) return { error: "That amount is too large." };
    return { minor };
  }

  function cleanHandle(text) {
    return String(text || "").trim().replace(/^@/, "").toLowerCase();
  }

  function equalSplit(amount, n) {
    const base = Math.floor(amount / n);
    const remainder = amount - base * n;
    return Array.from({ length: n }, (_, i) => base + (i < remainder ? 1 : 0));
  }

  const timeFmt = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  function when(iso) {
    const d = new Date(iso);
    return isNaN(d) ? iso : timeFmt.format(d);
  }

  // ---------------------------------------------------------------- session
  const session = {
    get token() { try { return localStorage.getItem(TOKEN_KEY); } catch (e) { return null; } },
    set(token) { localStorage.setItem(TOKEN_KEY, token); },
    clear() { try { localStorage.removeItem(TOKEN_KEY); } catch (e) { /* ignore */ } },
  };

  class Uncertain extends Error {}

  async function api(method, path, opts) {
    const { body, key, auth = true } = opts || {};
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (auth && session.token) headers.Authorization = "Bearer " + session.token;
    if (key) headers["Idempotency-Key"] = key;
    let resp, text;
    try {
      resp = await fetch(path, {
        method, headers, cache: "no-store",
        body: body === undefined ? undefined : JSON.stringify(body),
      });
      text = await resp.text();
    } catch (e) {
      throw new Uncertain("The connection failed before we heard back.");
    }
    if (resp.status >= 500) throw new Uncertain("The server could not confirm the outcome.");
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch (e) {
        if (resp.ok) throw new Uncertain("The response could not be read.");
      }
    }
    if (resp.status === 401 && auth) {
      session.clear();
      location.assign("/login");
      throw new Uncertain("Your session has ended.");
    }
    return { status: resp.status, ok: resp.ok, data };
  }

  const MESSAGES = {
    insufficient_funds: "Not enough available funds. Money on hold can't be spent.",
    self_payment: "You can't send money to yourself.",
    self_request: "You can't request money from yourself.",
    request_not_pending: "This request is no longer pending — the list has been refreshed.",
    authorization_not_open: "This hold is no longer open — the list has been refreshed.",
    authorization_expired: "This hold has expired and its funds were released.",
    capture_exceeds_authorization: "That's more than the amount still on hold.",
    forbidden: "You're not allowed to do that.",
    email_taken: "An account with that email already exists.",
    handle_taken: "The handle made from that email is already taken. Try a different email.",
    unauthenticated: "That email and password don't match an account.",
    idempotency_key_reuse: "This form changed while a retry was pending. Please submit again.",
    missing_idempotency_key: "Something went wrong preparing the request. Please try again.",
    malformed_request: "Some details aren't in the expected format.",
  };
  function describe(result, notFound) {
    const code = result.data && result.data.error && result.data.error.code;
    if (code === "not_found") return notFound || "We couldn't find that.";
    if (code === "validation_failed") {
      const m = result.data.error.message;
      return m ? "Please check the details: " + m + "." : "Please check the details and try again.";
    }
    return MESSAGES[code] || "That didn't work. Please try again.";
  }

  // ------------------------------------------------------------ data stores
  let me = null;
  let resolveMe;
  const meReady = new Promise((r) => { resolveMe = r; });
  const seq = { me: 0, feed: 0, requests: 0, auths: 0 };
  const applied = { me: 0, feed: 0, requests: 0, auths: 0 };
  const listeners = { me: [], feed: [], requests: [], auths: [] };

  async function load(kind, path) {
    const mine = ++seq[kind];
    try {
      const r = await api("GET", path);
      if (!r.ok) throw new Error("load failed");
      if (mine <= applied[kind]) return;          // a later read already landed
      applied[kind] = mine;
      if (kind === "me") { me = r.data; resolveMe(me); }
      listeners[kind].forEach((fn) => fn(r.data, null));
    } catch (e) {
      if (mine <= applied[kind]) return;
      listeners[kind].forEach((fn) => fn(null, e));
    }
  }
  const refresh = {
    me: () => load("me", "/me"),
    feed: () => load("feed", "/activity?limit=50"),
    requests: () => load("requests", "/requests?limit=200"),
    auths: () => load("auths", "/authorizations?limit=200"),
  };
  function on(kind, fn) { listeners[kind].push(fn); }

  // ------------------------------------------------------------- components
  function slot(cls) { return h("div", { class: cls || "" }); }
  function setMsg(container, testid, kind, text) {
    container.replaceChildren(text ? h("div", { class: "msg " + kind, testid, role: kind === "ok" ? "status" : "alert" }, text) : "");
  }

  function header(active) {
    const who = h("div", { class: "who" });
    const links = [["/", "Wallet"], ["/requests", "Requests"], ["/split", "Split a bill"], ["/authorizations", "Holds"]];
    const nav = h("nav", { class: "nav", "aria-label": "Main" },
      links.map(([href, label]) => h("a", { href, "aria-current": href === active ? "page" : null }, label)));
    const bar = h("header", { class: "topbar" },
      h("div", { class: "topbar-inner" },
        h("a", { class: "brand", href: "/" }, h("img", { src: "/static/icon.svg", alt: "" }), "Pocketful"),
        session.token ? nav : null,
        who));
    if (session.token) {
      on("me", (data) => {
        if (!data) return;
        who.replaceChildren(
          h("span", { class: "avatar", "aria-hidden": "true" }, (data.display_name || data.handle || "?").slice(0, 1).toUpperCase()),
          h("span", { class: "names", testid: "current-user" },
            h("span", { class: "name" }, data.display_name),
            h("span", { class: "handle", testid: "current-handle" }, data.handle)),
          h("button", { class: "btn secondary small", type: "button", testid: "logout-button", onclick: () => { session.clear(); location.assign("/login"); } }, "Log out"));
      });
    } else {
      who.replaceChildren(
        h("a", { class: "btn secondary small", href: "/login" }, "Log in"),
        h("a", { class: "btn small", href: "/signup" }, "Sign up"));
    }
    return bar;
  }

  function balanceCard(compact) {
    const headline = h("div", { class: "headline" }, h("span", { class: "skeleton", "aria-label": "Loading balance" }));
    const secondary = h("div", { class: "secondary" });
    const card = h("section", { class: "card balance" + (compact ? " compact" : ""), "aria-label": "Your balance" },
      h("div", {}, h("div", { class: "label" }, "Available to spend"), headline, secondary),
      h("button", { class: "btn ghost", type: "button", testid: "wallet-refresh", onclick: () => { refresh.me(); if (!compact) refresh.feed(); } }, "Refresh"));
    on("me", (data, err) => {
      if (!data) {
        if (!headline.querySelector("[data-testid]")) secondary.replaceChildren(h("span", {}, "Couldn't load your balance. Try Refresh."));
        return;
      }
      const f = (n) => money(n, data.minor_units, data.currency);
      headline.replaceChildren(h("span", { testid: "wallet-available", "data-amount": String(data.available) }, f(data.available)));
      secondary.replaceChildren(
        h("span", {}, h("span", { class: "k" }, "Total"), h("span", { testid: "wallet-balance", "data-amount": String(data.balance) }, f(data.balance))),
        data.held > 0 ? h("span", { class: "held-v" }, h("span", { class: "k" }, "On hold"), h("span", { testid: "wallet-held", "data-amount": String(data.held) }, f(data.held))) : null);
    });
    return card;
  }

  function amountField(id, testid, label) {
    const input = h("input", { id, testid, inputmode: "decimal", autocomplete: "off", placeholder: "0.00", required: true });
    const cur = h("span", { class: "cur", "aria-hidden": "true" });
    meReady.then((m) => { cur.textContent = m.currency; input.placeholder = m.minor_units ? "0." + "0".repeat(m.minor_units) : "0"; });
    return { input, el: h("div", { class: "field" }, h("label", { for: id }, label), h("div", { class: "amount-input" }, input, cur)) };
  }
  function textField(id, testid, label, attrs) {
    const input = h("input", Object.assign({ id, testid, autocomplete: "off" }, attrs || {}));
    return { input, el: h("div", { class: "field" }, h("label", { for: id }, label), input) };
  }
  function visibilityField(id, testid) {
    const input = h("select", { id, testid },
      h("option", { value: "public" }, "Public — anyone can see it"),
      h("option", { value: "private" }, "Private — only the two of you"));
    return { input, el: h("div", { class: "field" }, h("label", { for: id }, "Who can see it"), input) };
  }

  /**
   * Generic money-moving form. `build(values, m)` returns {body} or {error}.
   * The submit always reuses the retry identity for an unchanged body.
   */
  function moneyForm(opts) {
    const errSlot = slot(), uncSlot = slot(), okSlot = slot();
    const submit = h("button", { class: "btn block", type: "submit", testid: opts.prefix + "-submit" }, opts.submitLabel);
    const form = h("form", { novalidate: true, "aria-label": opts.title }, opts.fields.map((f) => f.el), submit, errSlot, uncSlot, okSlot);
    let busy = false;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (busy) return;
      const m = await meReady;
      okSlot.replaceChildren();
      const built = opts.build(m);
      if (built.error) {
        setMsg(errSlot, opts.prefix + "-error", "error", built.error);
        setMsg(uncSlot, null, "uncertain", "");
        return;
      }
      const key = identity(opts.prefix).keyFor(opts.path, built.body);
      busy = true; submit.disabled = true;
      try {
        const r = await api("POST", opts.path, { body: built.body, key });
        if (r.ok) {
          setMsg(errSlot, null, "error", "");
          setMsg(uncSlot, null, "uncertain", "");
          setMsg(okSlot, opts.prefix + "-success", "ok", opts.success(r.data, m));
          if (opts.after) opts.after(r.data);
        } else {
          setMsg(uncSlot, null, "uncertain", "");
          setMsg(errSlot, opts.prefix + "-error", "error", describe(r, opts.notFound));
          if (opts.afterRefusal) opts.afterRefusal(r);
        }
      } catch (err) {
        if (!(err instanceof Uncertain)) throw err;
        setMsg(errSlot, null, "error", "");
        setMsg(uncSlot, opts.prefix + "-uncertain", "uncertain",
          "We couldn't confirm whether this went through. Nothing will be duplicated — press “" + opts.submitLabel + "” again to check and finish safely.");
      } finally {
        busy = false; submit.disabled = false;
      }
    });
    return h("section", { class: "card" }, h("h2", {}, opts.title), opts.hint ? h("p", { class: "hint" }, opts.hint) : null, form);
  }

  function payForm() {
    const handle = textField("pay-handle", "pay-handle", "To (handle)", { placeholder: "e.g. bob", autocapitalize: "none", spellcheck: "false" });
    const amount = amountField("pay-amount", "pay-amount", "Amount");
    const note = textField("pay-note", "pay-note", "Note (optional)", { maxlength: "200", placeholder: "What's it for?" });
    const vis = visibilityField("pay-visibility", "pay-visibility");
    return moneyForm({
      prefix: "pay", title: "Send money", submitLabel: "Send", path: "/payments",
      fields: [handle, amount, note, vis], notFound: "No one has that handle.",
      build: (m) => {
        const to = cleanHandle(handle.input.value);
        if (!to) return { error: "Enter who you're paying." };
        const a = parseAmount(amount.input.value, m.minor_units);
        if (a.error) return a;
        return { body: { to_handle: to, amount: a.minor, note: note.input.value, visibility: vis.input.value } };
      },
      success: (p, m) => "Sent " + money(p.amount, m.minor_units, m.currency) + " to @" + p.to_handle + ".",
      after: () => { refresh.me(); refresh.feed(); },
      afterRefusal: () => { refresh.me(); refresh.feed(); },
    });
  }

  function requestForm() {
    const handle = textField("request-handle", "request-handle", "From (handle)", { placeholder: "e.g. ada", autocapitalize: "none", spellcheck: "false" });
    const amount = amountField("request-amount", "request-amount", "Amount");
    const note = textField("request-note", "request-note", "Note (optional)", { maxlength: "200", placeholder: "What's it for?" });
    return moneyForm({
      prefix: "request", title: "Request money", submitLabel: "Request", path: "/requests",
      fields: [handle, amount, note], notFound: "No one has that handle.",
      hint: "They'll see it in their requests and can pay when ready.",
      build: (m) => {
        const from = cleanHandle(handle.input.value);
        if (!from) return { error: "Enter who you're asking." };
        const a = parseAmount(amount.input.value, m.minor_units);
        if (a.error) return a;
        return { body: { payer_handle: from, amount: a.minor, note: note.input.value } };
      },
      success: (r, m) => "Asked @" + r.payer_handle + " for " + money(r.amount, m.minor_units, m.currency) + ".",
      after: () => { refresh.requests(); },
    });
  }

  function authorizeForm(afterAll) {
    const handle = textField("authorize-handle", "authorize-handle", "For (handle)", { placeholder: "e.g. bob", autocapitalize: "none", spellcheck: "false" });
    const amount = amountField("authorize-amount", "authorize-amount", "Amount to hold");
    const note = textField("authorize-note", "authorize-note", "Note (optional)", { maxlength: "200", placeholder: "e.g. deposit" });
    const vis = visibilityField("authorize-visibility", "authorize-visibility");
    return moneyForm({
      prefix: "authorize", title: "Hold money for someone", submitLabel: "Place hold", path: "/authorizations",
      fields: [handle, amount, note, vis], notFound: "No one has that handle.",
      hint: "Reserve funds now; they collect up to this amount later. Unclaimed money returns to you when the hold ends.",
      build: (m) => {
        const to = cleanHandle(handle.input.value);
        if (!to) return { error: "Enter who the hold is for." };
        const a = parseAmount(amount.input.value, m.minor_units);
        if (a.error) return a;
        return { body: { to_handle: to, amount: a.minor, note: note.input.value, visibility: vis.input.value } };
      },
      success: (a, m) => "Holding " + money(a.amount, m.minor_units, m.currency) + " for @" + a.to_handle + ".",
      after: () => { refresh.me(); refresh.auths(); if (afterAll) afterAll(); },
      afterRefusal: () => { refresh.me(); },
    });
  }

  function feedCard() {
    const body = h("div", { class: "loading" }, "Loading activity…");
    on("feed", async (data) => {
      if (!data) { body.replaceChildren(h("div", { class: "msg error", role: "alert" }, "Couldn't load activity. Try Refresh.")); return; }
      const m = await meReady;
      const mine = m.user_id;
      if (!data.payments.length) {
        body.replaceChildren(h("div", { class: "empty", testid: "empty-activity" }, h("strong", {}, "No activity yet"), "Payments you send, receive or that are shared publicly will appear here."));
        return;
      }
      body.replaceChildren(h("ul", { class: "list", testid: "activity-list" }, data.payments.map((p) => {
        const incoming = p.to_user_id === mine;
        const tags = [];
        if (p.request_id) tags.push(h("span", { class: "chip" }, "Request"));
        if (p.settlement_id) tags.push(h("span", { class: "chip" }, "Settlement"));
        if (p.authorization_id) tags.push(h("span", { class: "chip" }, "From hold"));
        return h("li", { class: "item", testid: "activity-item-" + p.payment_id, "data-visibility": p.visibility },
          h("span", { class: "dot" + (incoming ? " in" : ""), "aria-hidden": "true" }, incoming ? "↓" : "↑"),
          h("div", { class: "main" },
            h("div", { class: "parties", testid: "activity-parties-" + p.payment_id }, "@" + p.from_handle + " → @" + p.to_handle),
            h("div", { class: "note", testid: "activity-note-" + p.payment_id }, p.note)),
          h("div", { class: "amt" + (incoming ? " in" : ""), testid: "activity-amount-" + p.payment_id }, money(p.amount, m.minor_units, p.currency || m.currency)),
          h("div", { class: "meta" },
            h("time", { datetime: p.created_at, title: p.created_at }, when(p.created_at)),
            h("span", { class: "chip " + p.visibility }, p.visibility === "private" ? "Private" : "Public"),
            tags));
      })));
    });
    return h("section", { class: "card" }, h("div", { class: "section-head" }, h("h2", {}, "Activity")), body);
  }

  // ---------------------------------------------------------------- screens
  function walletScreen(main) {
    main.append(
      h("h1", { class: "sr-only" }, "Wallet"),
      balanceCard(false),
      h("div", { class: "grid", style: "margin-top:16px" }, payForm(), requestForm(), authorizeForm()),
      h("div", { style: "margin-top:16px" }, feedCard()));
    refresh.feed();
  }

  function requestsScreen(main) {
    const errSlot = slot();
    const incoming = h("ul", { class: "list", testid: "incoming-list", "aria-label": "Requests to you" });
    const outgoing = h("ul", { class: "list", testid: "outgoing-list", "aria-label": "Requests you sent" });
    const emptyBoth = slot();
    const body = h("div", { class: "loading" }, "Loading requests…");
    main.append(h("h1", {}, "Requests"), balanceCard(true), errSlot, body);

    async function act(r, action, payBody) {
      const path = "/requests/" + encodeURIComponent(r.request_id) + "/" + action;
      try {
        const res = action === "pay"
          ? await api("POST", path, { body: payBody, key: identity("request-pay-" + r.request_id).keyFor(path, payBody) })
          : await api("POST", path, {});
        if (res.ok) setMsg(errSlot, null, "error", "");
        else setMsg(errSlot, "request-error", "error", describe(res, "This request no longer exists."));
      } catch (err) {
        if (!(err instanceof Uncertain)) throw err;
        setMsg(errSlot, "request-error", "uncertain", "We couldn't confirm that action. Try again — a payment will never be made twice.");
      }
      refresh.requests(); refresh.me();
    }

    function item(r, direction, m) {
      const pending = r.status === "pending";
      const other = direction === "incoming" ? r.requester_handle : r.payer_handle;
      const label = direction === "incoming" ? "@" + other + " asked you" : "You asked @" + other;
      const actions = [];
      if (pending && direction === "incoming") {
        const vis = h("select", { "aria-label": "Payment visibility", class: "small" },
          h("option", { value: "public" }, "Public"), h("option", { value: "private" }, "Private"));
        actions.push(
          h("button", { class: "btn small", type: "button", testid: "request-pay-" + r.request_id, onclick: (e) => { e.target.disabled = true; act(r, "pay", { visibility: vis.value }); } }, "Pay " + money(r.amount, m.minor_units, m.currency)),
          vis,
          h("button", { class: "btn danger small", type: "button", testid: "request-decline-" + r.request_id, onclick: (e) => { e.target.disabled = true; act(r, "decline"); } }, "Decline"));
      }
      if (pending && direction === "outgoing") {
        actions.push(h("button", { class: "btn secondary small", type: "button", testid: "request-cancel-" + r.request_id, onclick: (e) => { e.target.disabled = true; act(r, "cancel"); } }, "Cancel request"));
      }
      return h("li", { class: "item", testid: "request-item-" + r.request_id, "data-status": r.status },
        h("span", { class: "dot" + (direction === "outgoing" ? " in" : ""), "aria-hidden": "true" }, (other || "?").slice(0, 1).toUpperCase()),
        h("div", { class: "main" }, h("div", { class: "parties" }, label), h("div", { class: "note" }, r.note)),
        h("div", { class: "amt", testid: "request-amount-" + r.request_id }, money(r.amount, m.minor_units, r.currency || m.currency)),
        h("div", { class: "meta" }, h("span", { class: "chip " + r.status }, r.status[0].toUpperCase() + r.status.slice(1)), h("time", { datetime: r.created_at }, when(r.created_at))),
        actions.length ? h("div", { class: "actions" }, actions) : null);
    }

    on("requests", async (data) => {
      if (!data) { body.replaceChildren(h("div", { class: "msg error", role: "alert" }, "Couldn't load requests. Please reload.")); return; }
      const m = await meReady;
      const mine = m.user_id;
      const inc = data.requests.filter((r) => r.payer_id === mine);
      const out = data.requests.filter((r) => r.requester_id === mine);
      incoming.replaceChildren(...inc.map((r) => item(r, "incoming", m)));
      outgoing.replaceChildren(...out.map((r) => item(r, "outgoing", m)));
      const none = (text) => h("p", { class: "loading" }, text);
      emptyBoth.replaceChildren(!inc.length && !out.length
        ? h("div", { class: "empty", testid: "empty-requests" }, h("strong", {}, "No requests yet"), "Ask someone for money from your wallet, or split a bill.")
        : "");
      body.replaceChildren(emptyBoth, h("div", { class: "grid" },
        h("section", { class: "card" }, h("h2", {}, "Requests to you"), incoming, inc.length ? null : none("Nothing to pay.")),
        h("section", { class: "card" }, h("h2", {}, "Requests you sent"), outgoing, out.length ? null : none("You haven't asked anyone."))));
    });
    refresh.requests();
  }

  function splitScreen(main) {
    const amount = amountField("split-amount", "split-amount", "Total you paid");
    const handles = textField("split-handles", "split-handles", "Who shared it (handles, comma-separated, in order)", { placeholder: "ada, bob, cy", autocapitalize: "none", spellcheck: "false" });
    const note = textField("split-note", "split-note", "Note (optional)", { maxlength: "200", placeholder: "e.g. dinner" });
    const preview = slot();

    function parsedHandles() {
      return String(handles.input.value || "").split(",").map(cleanHandle).filter(Boolean);
    }
    async function renderPreview() {
      const m = await meReady;
      const a = parseAmount(amount.input.value, m.minor_units);
      const list = parsedHandles();
      if (a.error || !list.length || new Set(list).size !== list.length) { preview.replaceChildren(); return; }
      const shares = equalSplit(a.minor, list.length);
      preview.replaceChildren(h("div", { class: "field" },
        h("span", { class: "hint" }, "Each person's share"),
        h("ul", { class: "shares", testid: "split-preview" }, list.map((hd, i) =>
          h("li", { class: hd === m.handle ? "me" : "" },
            h("span", {}, "@" + hd + (hd === m.handle ? " (you)" : "")),
            h("strong", { testid: "split-share-" + hd }, money(shares[i], m.minor_units, m.currency)))))));
    }
    amount.input.addEventListener("input", renderPreview);
    handles.input.addEventListener("input", renderPreview);

    const form = moneyForm({
      prefix: "split", title: "Split a bill", submitLabel: "Send requests", path: "/splits",
      fields: [amount, handles, note, { el: preview }], notFound: "One of those handles doesn't belong to anyone.",
      hint: "Everyone except you gets a request for their share. Uneven cents go to the first people listed.",
      build: (m) => {
        const a = parseAmount(amount.input.value, m.minor_units);
        if (a.error) return a;
        const list = parsedHandles();
        if (!list.length) return { error: "Add at least one handle." };
        if (new Set(list).size !== list.length) return { error: "Each person can appear only once." };
        return { body: { amount: a.minor, participant_handles: list, note: note.input.value } };
      },
      success: (s, m) => s.requests.length
        ? "Requested " + s.requests.map((r) => money(r.amount, m.minor_units, m.currency) + " from @" + r.payer_handle).join(", ") + "."
        : "Split recorded — there was no one else to ask.",
    });
    main.append(h("h1", {}, "Split a bill"), h("div", { class: "stack", style: "max-width:560px" }, form));
  }

  function authorizationsScreen(main) {
    const errSlot = slot();
    const body = h("div", { class: "loading" }, "Loading holds…");
    main.append(h("h1", {}, "Holds"), balanceCard(true),
      h("div", { class: "grid" }, authorizeForm(),
        h("section", { class: "card" }, h("h2", {}, "Your holds"), errSlot, body)));

    async function act(a, path, payload, key) {
      try {
        const res = await api("POST", path, payload === undefined ? {} : { body: payload, key });
        if (res.ok) setMsg(errSlot, null, "error", "");
        else setMsg(errSlot, "authorization-error", "error", describe(res, "This hold no longer exists."));
      } catch (err) {
        if (!(err instanceof Uncertain)) throw err;
        setMsg(errSlot, "authorization-error", "uncertain", "We couldn't confirm that action. Try again — money will never move twice.");
      }
      refresh.auths(); refresh.me();
    }

    function item(a, m) {
      const incoming = a.to_user_id === m.user_id;
      const f = (n) => money(n, m.minor_units, a.currency || m.currency);
      const label = incoming ? "@" + a.from_handle + " is holding for you" : "You're holding for @" + a.to_handle;
      const actions = [];
      if (a.status === "open" && incoming) {
        const amt = h("input", { id: "cap-" + a.authorization_id, testid: "authorization-capture-amount-" + a.authorization_id, inputmode: "decimal", value: plainDecimal(a.remaining_amount, m.minor_units), "aria-label": "Amount to collect" });
        const keep = h("input", { type: "checkbox", id: "keep-" + a.authorization_id });
        actions.push(
          h("div", { class: "field" }, amt),
          h("button", { class: "btn small", type: "button", testid: "authorization-capture-" + a.authorization_id, onclick: () => {
            const p = parseAmount(amt.value, m.minor_units);
            if (p.error) { setMsg(errSlot, "authorization-error", "error", p.error); return; }
            const payload = keep.checked ? { amount: p.minor, final: false } : { amount: p.minor };
            const path = "/authorizations/" + encodeURIComponent(a.authorization_id) + "/capture";
            act(a, path, payload, identity("capture-" + a.authorization_id).keyFor(path, payload));
          } }, "Collect"),
          h("label", { class: "check", for: "keep-" + a.authorization_id }, keep, "Keep the rest on hold"));
      }
      if (a.status === "open" && !incoming) {
        actions.push(h("button", { class: "btn danger small", type: "button", testid: "authorization-void-" + a.authorization_id, onclick: (e) => {
          e.target.disabled = true;
          act(a, "/authorizations/" + encodeURIComponent(a.authorization_id) + "/void");
        } }, "Release hold"));
      }
      return h("li", { class: "item", testid: "authorization-item-" + a.authorization_id, "data-status": a.status },
        h("span", { class: "dot" + (incoming ? " in" : ""), "aria-hidden": "true" }, incoming ? "↓" : "⏸"),
        h("div", { class: "main" }, h("div", { class: "parties" }, label), h("div", { class: "note" }, a.note)),
        h("div", { class: "amt", testid: "authorization-amount-" + a.authorization_id }, f(a.amount)),
        h("div", { class: "meta" },
          h("span", { class: "chip " + a.status }, a.status[0].toUpperCase() + a.status.slice(1)),
          a.status === "captured" ? h("span", {}, "Collected ", h("strong", { testid: "authorization-captured-" + a.authorization_id }, f(a.captured_amount))) : null,
          a.status === "open" && a.captured_amount > 0 ? h("span", {}, "Collected so far " + f(a.captured_amount) + ", " + f(a.remaining_amount) + " still held") : null,
          h("span", {}, "Expires ", h("time", { class: "mono", datetime: a.expires_at, title: when(a.expires_at), testid: "authorization-expires-" + a.authorization_id }, a.expires_at)),
          a.visibility === "private" ? h("span", { class: "chip private" }, "Private") : null),
        actions.length ? h("div", { class: "actions" }, actions) : null);
    }

    on("auths", async (data) => {
      if (!data) { body.replaceChildren(h("div", { class: "msg error", role: "alert" }, "Couldn't load holds. Please reload.")); return; }
      const m = await meReady;
      if (!data.authorizations.length) {
        body.replaceChildren(h("div", { class: "empty", testid: "empty-authorizations" }, h("strong", {}, "No holds"), "Holds you place or receive will appear here."));
        return;
      }
      body.replaceChildren(h("ul", { class: "list", testid: "authorization-list" }, data.authorizations.map((a) => item(a, m))));
    });
    refresh.auths();
  }

  function authScreen(main, mode) {
    const errSlot = slot();
    const isSignup = mode === "signup";
    const fields = isSignup
      ? [textField("signup-display-name", "signup-display-name", "Your name", { autocomplete: "name" }),
         textField("signup-email", "signup-email", "Email", { type: "email", autocomplete: "email", autocapitalize: "none" }),
         textField("signup-password", "signup-password", "Password (at least 8 characters)", { type: "password", autocomplete: "new-password" })]
      : [textField("login-email", "login-email", "Email", { type: "email", autocomplete: "email", autocapitalize: "none" }),
         textField("login-password", "login-password", "Password", { type: "password", autocomplete: "current-password" })];
    const submit = h("button", { class: "btn block", type: "submit", testid: mode + "-submit" }, isSignup ? "Create account" : "Log in");
    const form = h("form", { novalidate: true }, fields.map((f) => f.el), submit, errSlot);
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      submit.disabled = true;
      const body = isSignup
        ? { display_name: fields[0].input.value, email: fields[1].input.value.trim(), password: fields[2].input.value }
        : { email: fields[0].input.value.trim(), password: fields[1].input.value };
      try {
        const r = await api("POST", isSignup ? "/auth/signup" : "/auth/login", { body, auth: false });
        if (r.ok) {
          session.set(r.data.token);
          location.assign("/");
          return;
        }
        setMsg(errSlot, "auth-error", "error", describe(r));
      } catch (err) {
        setMsg(errSlot, "auth-error", "error", "We couldn't reach the server. Please try again.");
      }
      submit.disabled = false;
    });
    main.append(h("div", { class: "auth-wrap" }, h("section", { class: "card" },
      h("h1", {}, isSignup ? "Create your wallet" : "Welcome back"),
      h("p", { class: "hint" }, isSignup ? "Send, request and split money with people by handle." : "Log in to see your balance and activity."),
      form,
      h("p", { class: "hint", style: "margin:14px 0 0" }, isSignup ? "Already have an account? " : "New here? ",
        h("a", { href: isSignup ? "/login" : "/signup" }, isSignup ? "Log in" : "Create an account")))));
  }

  // ----------------------------------------------------------------- router
  const path = location.pathname.replace(/\/+$/, "") || "/";
  const screens = { "/": walletScreen, "/requests": requestsScreen, "/split": splitScreen, "/authorizations": authorizationsScreen };
  if (path === "/login" || path === "/signup") {
    const main = h("main", { id: "main" });
    app.replaceChildren(header(path), main);
    authScreen(main, path.slice(1));
    if (session.token) refresh.me();
  } else if (!session.token) {
    location.replace("/login");
  } else {
    const main = h("main", { id: "main" });
    app.replaceChildren(header(path), main);
    (screens[path] || walletScreen)(main);
    refresh.me();
  }
})();
