// HomeSense device radar (/map): polls /api/map and draws devices around the router.
// Only the distance is estimated (from RSSI); the angle comes from the server and is arbitrary but
// stable per device. Security: device names are attacker-controlled (DHCP hostnames), so all text is
// set with textContent; no HTML is ever built from server data.
"use strict";

(() => {
  const SVG = "http://www.w3.org/2000/svg";
  const RADIUS = 300; // px in the viewBox for MAX_M
  const MAX_M = 30;
  const REFRESH_MS = 10000;
  const TRAIL_MINUTES = 30;
  const COLOR = /^#[0-9a-fA-F]{6}$/;
  const GLYPHS = { phone: "T", computer: "K", tablet: "Tb", tv: "TV", iot: "I", unknown: "?" };
  const LABEL_MAX = 18;

  let labels = {};
  let timer = null;
  let inFlight = false;

  // Square-root scale: near devices get more room than with a linear scale.
  const radiusFor = (meters) => RADIUS * Math.sqrt(Math.min(MAX_M, Math.max(0, meters)) / MAX_M);

  function point(meters, angleDeg) {
    const r = radiusFor(meters);
    const a = (angleDeg * Math.PI) / 180;
    return [r * Math.sin(a), -r * Math.cos(a)];
  }

  function el(name, attrs = {}) {
    const node = document.createElementNS(SVG, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
    return node;
  }

  function shortName(name) {
    return name.length > LABEL_MAX ? `${name.slice(0, LABEL_MAX - 1)}…` : name;
  }

  function formatTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" });
  }

  function tooltip(dev) {
    const lines = [dev.name];
    if (dev.person_name) lines.push(`Osoba: ${dev.person_name}`);
    lines.push(`Typ: ${labels[dev.kind] || dev.kind}${dev.kind_guessed ? " (zgadnięty)" : ""}`);
    if (dev.wired) {
      lines.push("Połączenie przewodowe — pozycja przy routerze");
    } else {
      lines.push(`RSSI: ${dev.rssi} dBm (wygładzone ${dev.rssi_smoothed} dBm)`);
      lines.push(`Odległość: ok. ${dev.distance_m.toFixed(1)} m`);
    }
    lines.push(`${dev.online ? "Online" : "Offline"}, ostatnio: ${formatTime(dev.last_seen)}`);
    return lines.join("\n");
  }

  function makeDot(mac) {
    const g = el("g", { class: "radar-dot", tabindex: 0 });
    g.dataset.mac = mac;
    g.append(el("title"), el("circle", { r: 12 }), el("text", { class: "radar-glyph" }), el("text", { class: "radar-label", y: 26 }));
    return g;
  }

  function updateDot(g, dev) {
    const [x, y] = point(dev.distance_m, dev.angle_deg);
    g.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    g.classList.toggle("is-offline", !dev.online);
    g.classList.toggle("is-wired", Boolean(dev.wired));
    const [title, circle, glyph, label] = g.children;
    title.textContent = tooltip(dev);
    circle.style.fill = dev.color && COLOR.test(dev.color) ? dev.color : "";
    glyph.textContent = GLYPHS[dev.kind] || "?";
    label.textContent = shortName(dev.name);
  }

  function drawTrails(devices, show) {
    const layer = document.getElementById("radar-trails");
    layer.replaceChildren();
    if (!show) return;
    for (const dev of devices) {
      if (dev.wired || !dev.trail || dev.trail.length < 2) continue;
      const points = dev.trail.map((p) => point(p.distance_m, dev.angle_deg).map((v) => v.toFixed(1)).join(",")).join(" ");
      const line = el("polyline", { class: "radar-trail", points });
      if (dev.color && COLOR.test(dev.color)) line.style.stroke = dev.color;
      layer.append(line);
    }
  }

  function drawNoSignal(devices) {
    const list = document.getElementById("no-signal");
    const items = devices.map((dev) => {
      const li = document.createElement("li");
      const badge = document.createElement("span");
      badge.className = `kind-badge kind-${GLYPHS[dev.kind] ? dev.kind : "unknown"}`;
      badge.setAttribute("aria-hidden", "true");
      if (dev.color && COLOR.test(dev.color)) badge.style.background = dev.color;
      const name = document.createElement("span");
      name.textContent = dev.name + (dev.online ? "" : " (offline)");
      name.title = `${labels[dev.kind] || dev.kind}, ostatnio: ${formatTime(dev.last_seen)}`;
      li.append(badge, name);
      return li;
    });
    list.replaceChildren(...items);
    document.getElementById("no-signal-empty").hidden = items.length > 0;
  }

  function render(devices, showTrail) {
    const placed = devices.filter((d) => d.distance_m !== null && d.distance_m !== undefined);
    const layer = document.getElementById("radar-dots");
    const existing = new Map([...layer.children].map((g) => [g.dataset.mac, g]));
    for (const dev of placed) {
      let g = existing.get(dev.mac);
      if (g) existing.delete(dev.mac);
      else {
        g = makeDot(dev.mac);
        layer.append(g);
      }
      updateDot(g, dev);
    }
    for (const g of existing.values()) g.remove();
    drawTrails(placed, showTrail);
    drawNoSignal(devices.filter((d) => d.distance_m === null || d.distance_m === undefined));
    document.getElementById("radar-empty").hidden = placed.length > 0;
  }

  function query(form) {
    const params = new URLSearchParams();
    const kinds = [...form.querySelectorAll('input[name="kind"]:checked')].map((i) => i.value);
    params.set("kinds", kinds.length ? kinds.join(",") : "none");
    if (form.person_id.value) params.set("person_id", form.person_id.value);
    if (form.online_only.checked) params.set("online_only", "1");
    params.set("trail_minutes", form.trail.checked ? String(TRAIL_MINUTES) : "0");
    return params;
  }

  async function refresh() {
    const form = document.getElementById("map-filters");
    const status = document.getElementById("map-status");
    if (!form || inFlight) return;
    const params = query(form);
    if (params.get("kinds") === "none") {
      render([], false);
      status.textContent = "Nie wybrano żadnego typu urządzenia.";
      return;
    }
    inFlight = true;
    try {
      const response = await fetch(`/api/map?${params}`, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.status === 401) {
        window.location.assign("/login");
        return;
      }
      if (!response.ok) throw new Error(String(response.status));
      const devices = await response.json();
      render(devices, form.trail.checked);
      status.textContent = `Aktualizacja: ${new Date().toLocaleTimeString("pl-PL")}`;
    } catch {
      status.textContent = "Nie udało się pobrać danych — ponowię za chwilę.";
    } finally {
      inFlight = false;
    }
  }

  function readLabels() {
    try {
      return JSON.parse(document.getElementById("map-kind-labels").textContent) || {};
    } catch {
      return {};
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("map-filters");
    if (!form) return;
    labels = readLabels();
    form.addEventListener("change", refresh);
    form.addEventListener("submit", (e) => e.preventDefault());
    document.body.addEventListener("hsrefresh", refresh); // presence events from the SSE stream
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
    });
    refresh();
    timer = setInterval(() => {
      if (!document.hidden) refresh();
    }, REFRESH_MS);
    window.addEventListener("pagehide", () => clearInterval(timer));
  });
})();
