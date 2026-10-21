// HomeSense panel: HTMX JSON forms, live events (SSE), charts, person colors, PWA registration.
// Security: text from the server (names, hostnames, domains, alert texts) is only ever inserted
// with textContent, never innerHTML. The CSRF token comes from <meta name="csrf-token">.
"use strict";

(() => {
  const csrfToken = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  function readJson(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch {
      return null;
    }
  }

  // --- HTMX: JSON request bodies for /api/* ---------------------------------------------------
  //
  // hx-ext="hs-json" sends the form (or hx-vals) as JSON. Form fields: checkbox -> boolean,
  // data-type="int" -> integer or null when empty, data-type="nullable" -> string or null when
  // empty (e.g. device kind: the API rejects ""), type="number" -> number or null.

  function formObject(form) {
    const out = {};
    for (const el of form.elements) {
      if (!el.name || el.disabled || el.name === "csrf_token") continue;
      const type = el.dataset.type;
      if (el.type === "checkbox") out[el.name] = el.checked;
      else if (type === "int") out[el.name] = el.value === "" ? null : parseInt(el.value, 10);
      else if (type === "nullable") out[el.name] = el.value === "" ? null : el.value;
      else if (el.type === "number") out[el.name] = el.value === "" ? null : Number(el.value);
      else if (el.type !== "submit" && el.type !== "button") out[el.name] = el.value;
    }
    return out;
  }

  function defineJsonExtension() {
    htmx.defineExtension("hs-json", {
      onEvent(name, evt) {
        if (name === "htmx:configRequest") evt.detail.headers["Content-Type"] = "application/json";
      },
      encodeParameters(xhr, parameters, elt) {
        xhr.overrideMimeType("text/json");
        if (elt.tagName === "FORM") return JSON.stringify(formObject(elt));
        const out = {};
        parameters.forEach((value, key) => {
          if (key !== "csrf_token") out[key] = value;
        });
        return JSON.stringify(out);
      },
    });
  }

  function statusBox(elt) {
    const scope = elt.closest("form, .actions, .card") || elt.parentElement;
    return scope ? scope.querySelector(".form-status") : null;
  }

  function errorText(xhr) {
    if (xhr.status === 0) return "Brak połączenia z serwerem.";
    try {
      const detail = JSON.parse(xhr.responseText).detail;
      if (typeof detail === "string") return detail;
      if (Array.isArray(detail)) return "Nieprawidłowe dane w formularzu.";
    } catch {
      /* not JSON */
    }
    return `Błąd serwera (${xhr.status}).`;
  }

  // --- monitoring on/off switch -----------------------------------------------------------------

  function monitoringText(state) {
    if (!state.paused) return "Monitoring: włączony";
    if (!state.until) return "Monitoring: wstrzymany do odwołania";
    const d = new Date(state.until);
    if (Number.isNaN(d.getTime())) return "Monitoring: wstrzymany";
    const today = new Date().toDateString() === d.toDateString();
    const time = d.toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    const day = d.toLocaleDateString("pl-PL", { day: "2-digit", month: "2-digit" });
    return `Monitoring: wstrzymany do ${today ? time : `${day} ${time}`}`;
  }

  function applyMonitoring(state) {
    if (!state || typeof state.paused !== "boolean") return;
    for (const box of document.querySelectorAll("[data-monitoring-box]")) {
      box.classList.toggle("is-paused", state.paused);
      const text = box.querySelector(".js-mon-text");
      if (text) text.textContent = monitoringText(state);
      const dot = box.querySelector(".js-mon-dot");
      if (dot) dot.className = `dot ${state.paused ? "dot-warn" : "dot-on"} js-mon-dot`;
      const pause = box.querySelector(".js-mon-pause");
      if (pause) {
        pause.hidden = state.paused;
        pause.open = false;
      }
      const resume = box.querySelector(".js-mon-resume");
      if (resume) resume.hidden = !state.paused;
    }
  }

  // Every mutating htmx request (POST/PUT/PATCH/DELETE, with or without the hs-json extension)
  // reports its outcome: redirect / reload on success, the API's error text otherwise.
  function onAfterRequest(evt) {
    const elt = evt.detail.elt;
    const verb = (evt.detail.requestConfig && evt.detail.requestConfig.verb) || "get";
    if (!elt || verb.toLowerCase() === "get") return;
    if ("monitoring" in elt.dataset) {
      if (evt.detail.successful) {
        try {
          applyMonitoring(JSON.parse(evt.detail.xhr.responseText));
        } catch {
          /* not JSON */
        }
      }
      return;
    }
    const box = statusBox(elt);
    if (evt.detail.successful) {
      if (elt.dataset.redirect) {
        window.location.assign(elt.dataset.redirect);
      } else if ("reload" in elt.dataset) {
        window.location.reload();
      } else if (box) {
        box.textContent = elt.dataset.success || "Zapisano.";
        box.classList.remove("is-error");
      }
    } else if (box) {
      box.textContent = errorText(evt.detail.xhr);
      box.classList.add("is-error");
    }
  }

  // --- person colors (no inline style attributes under the CSP) ------------------------------

  const COLOR = /^#[0-9a-fA-F]{6}$/;

  function applyColors(root) {
    const nodes = root.querySelectorAll ? root.querySelectorAll("[data-color]") : [];
    for (const el of [root, ...nodes]) {
      const color = el.dataset && el.dataset.color;
      if (color && COLOR.test(color)) el.style.setProperty("--person-color", color);
    }
  }

  // --- charts ----------------------------------------------------------------------------------

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function renderCharts(root) {
    if (typeof Chart === "undefined") return;
    const palette = [cssVar("--chart-1"), cssVar("--chart-2"), cssVar("--chart-3")];
    const text = cssVar("--muted");
    const grid = cssVar("--border");
    for (const canvas of root.querySelectorAll("canvas[data-chart]")) {
      if (canvas.dataset.rendered) continue;
      const data = readJson(canvas.dataset.source);
      if (!data) continue;
      const type = canvas.dataset.chart;
      data.datasets.forEach((ds, i) => {
        const color = palette[i % palette.length];
        ds.backgroundColor = type === "line" ? color + "33" : color;
        ds.borderColor = color;
        ds.borderWidth = type === "line" ? 2 : 0;
        ds.pointRadius = 0;
        ds.fill = type === "line";
        ds.tension = 0.25;
      });
      new Chart(canvas, {
        type,
        data,
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { labels: { color: text, boxWidth: 12 } } },
          scales: {
            x: { ticks: { color: text, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 }, grid: { display: false } },
            y: { beginAtZero: true, ticks: { color: text, precision: 0 }, grid: { color: grid } },
          },
        },
      });
      canvas.dataset.rendered = "1";
    }
  }

  // --- live events (SSE) -----------------------------------------------------------------------

  function feedText(event, persons) {
    const d = event.data || {};
    if (event.type === "PresenceEvent") {
      const who = persons[String(d.person_id)] || `Osoba #${d.person_id}`;
      return { dot: d.kind === "arrive" ? "dot-on" : "dot-off", who, what: d.kind === "arrive" ? "przyjście do domu" : "wyjście z domu" };
    }
    if (event.type === "NewDeviceEvent") return { dot: "dot-warn", who: "Nowe urządzenie", what: String(d.mac || "") };
    if (event.type === "AlertRaised") {
      const a = d.alert || {};
      return { dot: a.severity === "critical" ? "dot-err" : "dot-warn", who: "Alert", what: String(a.title || "") };
    }
    return null;
  }

  function prependFeed(event) {
    const feed = document.getElementById("event-feed");
    if (!feed) return;
    const item = feedText(event, readJson("person-index") || {});
    if (!item) return;
    feed.querySelector(".feed-empty")?.remove();
    const li = document.createElement("li");
    li.className = "feed-item is-new";
    const time = document.createElement("span");
    time.className = "feed-time";
    time.textContent = new Date().toLocaleTimeString("pl-PL", { hour: "2-digit", minute: "2-digit" });
    const dot = document.createElement("span");
    dot.className = `dot ${item.dot}`;
    const body = document.createElement("span");
    const who = document.createElement("strong");
    who.textContent = item.who;
    body.append(who, document.createTextNode(` — ${item.what}`));
    li.append(time, dot, body);
    feed.prepend(li);
    while (feed.children.length > 30) feed.lastElementChild.remove();
  }

  function bumpBadge() {
    const badge = document.getElementById("alert-badge");
    if (!badge) return;
    badge.textContent = String((parseInt(badge.textContent, 10) || 0) + 1);
    badge.hidden = false;
  }

  function connectEvents() {
    if (!document.body.dataset.live || typeof EventSource === "undefined") return;
    const status = document.getElementById("live-status");
    const source = new EventSource("/api/events/stream");
    source.onopen = () => status?.classList.remove("is-offline");
    source.onerror = () => status?.classList.add("is-offline");
    source.onmessage = (msg) => {
      let event;
      try {
        event = JSON.parse(msg.data);
      } catch {
        return;
      }
      prependFeed(event);
      if (event.type === "AlertRaised") bumpBadge();
      if (event.type === "PresenceEvent") document.body.dispatchEvent(new Event("hsrefresh"));
      if (event.type === "MonitoringChanged") applyMonitoring(event.data);
    };
  }

  // --- Alpine: alert routing rules on the settings page ----------------------------------------

  document.addEventListener("alpine:init", () => {
    window.Alpine.data("alertRules", () => ({
      cfg: readJson("alert-rules-data") || { channels: {}, min_severity: "info", quiet_hours: { start: 23, end: 7, allow: [] } },
      labels: readJson("alert-kind-labels") || {},
      status: "",
      kinds() {
        return Object.keys(this.cfg.channels);
      },
      kindLabel(kind) {
        return this.labels[kind] || kind;
      },
      has(kind, channel) {
        return (this.cfg.channels[kind] || []).includes(channel);
      },
      toggle(kind, channel) {
        const list = this.cfg.channels[kind] || [];
        this.cfg.channels[kind] = list.includes(channel) ? list.filter((c) => c !== channel) : [...list, channel];
      },
      allows(severity) {
        return (this.cfg.quiet_hours.allow || []).includes(severity);
      },
      toggleAllow(severity) {
        const allow = this.cfg.quiet_hours.allow || [];
        this.cfg.quiet_hours.allow = allow.includes(severity) ? allow.filter((s) => s !== severity) : [...allow, severity];
      },
      async save() {
        this.status = "Zapisywanie…";
        try {
          const response = await fetch("/api/settings/alerts", {
            method: "PUT",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrfToken() },
            body: JSON.stringify(this.cfg),
          });
          this.status = response.ok ? "Zapisano." : errorText({ status: response.status, responseText: await response.text() });
        } catch {
          this.status = "Brak połączenia z serwerem.";
        }
      },
    }));
  });

  // --- analytics: Grafana availability ---------------------------------------------------------

  function checkGrafana() {
    const notice = document.getElementById("grafana-down");
    if (!notice) return;
    fetch("/grafana/api/health", { credentials: "same-origin" })
      .then((r) => {
        if (!r.ok) notice.hidden = false;
      })
      .catch(() => {
        notice.hidden = false;
      });
  }

  // --- start -----------------------------------------------------------------------------------

  // Deferred scripts run before DOMContentLoaded, i.e. before htmx processes the page, so the
  // extension and the htmx:load hook are in place for the initial content as well.
  if (window.htmx) {
    defineJsonExtension();
    document.body.addEventListener("htmx:afterRequest", onAfterRequest);
    htmx.onLoad((root) => {
      applyColors(root);
      renderCharts(root);
    });
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      applyColors(document.body);
      renderCharts(document.body);
    });
  }
  document.addEventListener("DOMContentLoaded", () => {
    connectEvents();
    checkGrafana();
    if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
  });
})();
