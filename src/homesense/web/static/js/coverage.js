// HomeSense Wi-Fi coverage (/coverage): floor plan editor, survey measurements and an IDW heatmap.
// Security: device and room names are user/attacker controlled, so text goes into the DOM only via
// textContent (and onto the canvas via fillText); no HTML is ever built from data.
"use strict";

(() => {
  const BEST = -30; // dBm -> green
  const WORST = -85; // dBm -> red
  const SNAP_M = 0.25;
  const CELL_PX = 8;
  const POLL_MS = 2000;
  const MIN_ROOM_M = 0.5;

  const state = { plan: null, points: [], drag: null, polling: null, bands: {} };
  const $ = (id) => document.getElementById(id);
  const csrf = () => document.querySelector('meta[name="csrf-token"]')?.content || "";

  // --- helpers ---------------------------------------------------------------------------------

  function cssVar(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }

  function color(rssi, alpha = 1) {
    const t = Math.min(1, Math.max(0, (rssi - WORST) / (BEST - WORST)));
    return `hsla(${Math.round(t * 120)}, 80%, 45%, ${alpha})`;
  }

  async function api(method, url, body) {
    const opts = { method, headers: { "X-CSRF-Token": csrf() }, credentials: "same-origin" };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const response = await fetch(url, opts);
    let data = null;
    if (response.status !== 204) {
      try {
        data = await response.json();
      } catch {
        data = null;
      }
    }
    if (!response.ok) {
      const detail = data && typeof data.detail === "string" ? data.detail : `Błąd serwera (${response.status}).`;
      throw new Error(detail);
    }
    return data;
  }

  function say(id, text, isError = false) {
    const el = $(id);
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("is-error", isError);
  }

  function selectedMode() {
    return document.querySelector('#coverage-modes input[name="mode"]:checked')?.value || "measure";
  }

  function bandLabel(band) {
    return state.bands[band] || band;
  }

  // --- geometry --------------------------------------------------------------------------------

  function toMetres(evt) {
    const canvas = $("coverage-canvas");
    const rect = canvas.getBoundingClientRect();
    const s = rect.width / state.plan.width_m;
    const snap = (v, max) => Math.min(max, Math.max(0, Math.round(v / s / SNAP_M) * SNAP_M));
    return { x: snap(evt.clientX - rect.left, state.plan.width_m), y: snap(evt.clientY - rect.top, state.plan.height_m) };
  }

  // --- drawing ---------------------------------------------------------------------------------

  function visiblePoints() {
    const band = $("band-filter").value;
    return state.points.filter((p) => p.status === "ok" && (band === "all" || p.band === band));
  }

  function idw(points, x, y) {
    let num = 0;
    let den = 0;
    for (const p of points) {
      const d2 = (p.x - x) ** 2 + (p.y - y) ** 2;
      if (d2 < 1e-4) return p.rssi;
      const w = 1 / d2;
      num += w * p.rssi;
      den += w;
    }
    return num / den;
  }

  function drawRouter(ctx, x, y) {
    const accent = cssVar("--accent", "#0f766e");
    ctx.fillStyle = accent;
    ctx.strokeStyle = accent;
    ctx.lineWidth = 3;
    ctx.lineCap = "round";
    ctx.fillRect(x - 12, y - 7, 24, 14);
    ctx.beginPath();
    ctx.moveTo(x - 6, y - 7);
    ctx.lineTo(x - 9, y - 16);
    ctx.moveTo(x + 6, y - 7);
    ctx.lineTo(x + 9, y - 16);
    ctx.stroke();
  }

  function render() {
    const canvas = $("coverage-canvas");
    const wrap = $("coverage-wrap");
    const plan = state.plan;
    $("coverage-empty").hidden = Boolean(plan);
    wrap.hidden = !plan;
    renderRooms();
    renderPoints();
    if (!plan) return;

    const width = wrap.clientWidth;
    const s = width / plan.width_m;
    const height = Math.round(plan.height_m * s);
    const dpr = window.devicePixelRatio || 1;
    canvas.style.height = `${height}px`;
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = cssVar("--surface-2", "#eef3f3");
    ctx.fillRect(0, 0, width, height);

    const points = visiblePoints();
    if (points.length) {
      for (let px = 0; px < width; px += CELL_PX) {
        for (let py = 0; py < height; py += CELL_PX) {
          const value = idw(points, (px + CELL_PX / 2) / s, (py + CELL_PX / 2) / s);
          ctx.fillStyle = color(value, 0.6);
          ctx.fillRect(px, py, CELL_PX, CELL_PX);
        }
      }
    }

    // 1 m grid
    ctx.strokeStyle = cssVar("--border", "#dbe3e4");
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let m = 1; m < plan.width_m; m += 1) {
      ctx.moveTo(Math.round(m * s) + 0.5, 0);
      ctx.lineTo(Math.round(m * s) + 0.5, height);
    }
    for (let m = 1; m < plan.height_m; m += 1) {
      ctx.moveTo(0, Math.round(m * s) + 0.5);
      ctx.lineTo(width, Math.round(m * s) + 0.5);
    }
    ctx.globalAlpha = 0.6;
    ctx.stroke();
    ctx.globalAlpha = 1;

    const text = cssVar("--text", "#142022");
    ctx.font = "13px system-ui, sans-serif";
    ctx.textBaseline = "top";
    for (const room of plan.rooms) {
      ctx.strokeStyle = text;
      ctx.lineWidth = 2;
      ctx.strokeRect(room.x * s, room.y * s, room.w * s, room.h * s);
      ctx.fillStyle = text;
      ctx.fillText(room.name, room.x * s + 5, room.y * s + 5, Math.max(10, room.w * s - 10));
    }
    if (state.drag) {
      const r = dragRect();
      ctx.setLineDash([6, 4]);
      ctx.strokeStyle = text;
      ctx.strokeRect(r.x * s, r.y * s, r.w * s, r.h * s);
      ctx.setLineDash([]);
    }
    if (plan.router) drawRouter(ctx, plan.router.x * s, plan.router.y * s);

    if ($("show-points").checked) {
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";
      ctx.font = "bold 10px system-ui, sans-serif";
      const band = $("band-filter").value;
      for (const p of state.points) {
        if (band !== "all" && p.band !== band && p.status === "ok") continue;
        const x = p.x * s;
        const y = p.y * s;
        ctx.beginPath();
        ctx.arc(x, y, 11, 0, 2 * Math.PI);
        ctx.fillStyle = p.status === "ok" ? color(p.rssi) : cssVar("--off", "#94a3b8");
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = cssVar("--surface", "#fff");
        if (p.status === "measuring") ctx.setLineDash([3, 3]);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = "#fff";
        ctx.fillText(p.status === "ok" ? String(Math.round(p.rssi)) : p.status === "measuring" ? "…" : "?", x, y);
      }
      ctx.textAlign = "start";
    }
  }

  function renderLegend() {
    const canvas = $("coverage-legend");
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    for (let i = 0; i < canvas.width; i += 1) {
      ctx.fillStyle = color(BEST + ((WORST - BEST) * i) / (canvas.width - 1));
      ctx.fillRect(i, 0, 1, canvas.height);
    }
  }

  function renderRooms() {
    const list = $("room-list");
    list.replaceChildren();
    if (!state.plan) return;
    state.plan.rooms.forEach((room, index) => {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = `${room.name} (${room.w} × ${room.h} m)`;
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-small btn-ghost";
      del.textContent = "Usuń";
      del.setAttribute("aria-label", `Usuń pokój ${room.name}`);
      del.addEventListener("click", () => {
        state.plan.rooms.splice(index, 1);
        savePlan();
      });
      li.append(name, del);
      list.append(li);
    });
  }

  function renderPoints() {
    const rows = $("point-rows");
    rows.replaceChildren();
    $("points-empty").hidden = state.points.length > 0;
    for (const p of [...state.points].reverse()) {
      const tr = document.createElement("tr");
      const cells = [
        new Date(p.started_at).toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" }),
        p.device,
        p.status === "ok" ? `${p.rssi} dBm` : p.status === "measuring" ? `pomiar… (${p.remaining_s} s)` : "brak próbek",
        String(p.samples),
        bandLabel(p.band),
      ];
      cells.forEach((value, i) => {
        const td = document.createElement("td");
        if (i === 2) td.className = "num";
        if (i === 3) td.className = "num hide-sm";
        td.textContent = value;
        tr.append(td);
      });
      const td = document.createElement("td");
      const del = document.createElement("button");
      del.type = "button";
      del.className = "btn btn-small btn-ghost";
      del.textContent = "Usuń";
      del.addEventListener("click", () => deletePoint(p.id));
      td.append(del);
      tr.append(td);
      rows.append(tr);
    }
  }

  // --- plan editing ----------------------------------------------------------------------------

  async function savePlan() {
    try {
      state.plan = await api("PUT", "/api/coverage/plan", state.plan);
      say("plan-status", "Zapisano plan.");
    } catch (err) {
      say("plan-status", err.message, true);
      await load();
      return;
    }
    render();
  }

  function dragRect() {
    const { start, end } = state.drag;
    return {
      x: Math.min(start.x, end.x),
      y: Math.min(start.y, end.y),
      w: Math.abs(end.x - start.x),
      h: Math.abs(end.y - start.y),
    };
  }

  function onPointerDown(evt) {
    if (!state.plan) return;
    const at = toMetres(evt);
    const mode = selectedMode();
    if (mode === "router") {
      state.plan.router = at;
      savePlan();
    } else if (mode === "room") {
      state.drag = { start: at, end: at };
      $("coverage-canvas").setPointerCapture(evt.pointerId);
    } else {
      startMeasurement(at);
    }
  }

  function onPointerMove(evt) {
    if (!state.drag) return;
    state.drag.end = toMetres(evt);
    render();
  }

  function onPointerUp() {
    if (!state.drag) return;
    const r = dragRect();
    state.drag = null;
    if (r.w < MIN_ROOM_M || r.h < MIN_ROOM_M) {
      render();
      return;
    }
    const input = $("room-name");
    const name = input.value.trim() || `Pokój ${state.plan.rooms.length + 1}`;
    input.value = "";
    state.plan.rooms.push({ name: name.slice(0, 40), ...r });
    savePlan();
  }

  function onPlanSubmit(evt) {
    evt.preventDefault();
    const form = evt.target;
    const width = Number(form.elements.width_m.value);
    const height = Number(form.elements.height_m.value);
    state.plan = { router: null, rooms: [], ...(state.plan || {}), width_m: width, height_m: height };
    savePlan();
  }

  // --- measurements ----------------------------------------------------------------------------

  async function startMeasurement(at) {
    const mac = $("survey-device").value;
    if (!mac) {
      say("survey-status", "Najpierw wybierz urządzenie, którym mierzysz.", true);
      return;
    }
    try {
      const point = await api("POST", "/api/coverage/points", {
        mac,
        x: at.x,
        y: at.y,
        window_s: Number($("survey-window").value),
      });
      state.points.push(point);
      render();
      watch(point.id);
    } catch (err) {
      say("survey-status", err.message, true);
    }
  }

  function describe(p) {
    if (p.status === "measuring") {
      const median = p.rssi === null ? "" : `, mediana ${p.rssi} dBm`;
      return `Pomiar trwa — nie ruszaj się. Zostało ${p.remaining_s} s, próbek: ${p.samples}${median}.`;
    }
    if (p.status === "ok") return `Zapisano: ${p.rssi} dBm (mediana z ${p.samples} próbek, pasmo ${bandLabel(p.band)}).`;
    return "Brak próbek z routera w tym czasie — punkt pominięty. Sprawdź, czy urządzenie jest połączone z Wi-Fi, i spróbuj ponownie.";
  }

  function watch(id) {
    clearTimeout(state.polling);
    const tick = async () => {
      let p;
      try {
        p = await api("GET", `/api/coverage/points/${id}`);
      } catch (err) {
        say("survey-status", err.message, true);
        return;
      }
      const index = state.points.findIndex((x) => x.id === id);
      if (index >= 0) state.points[index] = p;
      say("survey-status", describe(p), p.status === "no_samples");
      render();
      if (p.status === "measuring") state.polling = setTimeout(tick, POLL_MS);
    };
    tick();
  }

  async function deletePoint(id) {
    try {
      await api("DELETE", `/api/coverage/points/${id}`);
    } catch (err) {
      say("survey-status", err.message, true);
    }
    await load();
  }

  async function clearPoints() {
    if (!window.confirm("Usunąć wszystkie punkty pomiaru?")) return;
    try {
      await api("DELETE", "/api/coverage/points");
    } catch (err) {
      say("survey-status", err.message, true);
    }
    await load();
  }

  // --- signal over time (device x hour of day) ------------------------------------------------

  async function loadSignal() {
    const kinds = [...document.querySelectorAll('#signal-filters input[name="kind"]:checked')].map((i) => i.value);
    const rows = $("signal-rows");
    if (!kinds.length) {
      rows.replaceChildren();
      $("signal-empty").hidden = false;
      return;
    }
    let data;
    try {
      data = await api("GET", `/api/coverage/signal-hours?kinds=${encodeURIComponent(kinds.join(","))}`);
    } catch (err) {
      say("signal-status", err.message, true);
      return;
    }
    say("signal-status", "");
    rows.replaceChildren();
    $("signal-empty").hidden = data.devices.length > 0;
    for (const dev of data.devices) {
      const tr = document.createElement("tr");
      const name = document.createElement("th");
      name.scope = "row";
      name.className = "sg-name";
      name.textContent = dev.name;
      name.title = dev.mac;
      tr.append(name);
      dev.cells.forEach((cell, hour) => {
        const td = document.createElement("td");
        td.className = "sg-cell";
        if (cell) {
          td.style.backgroundColor = color(cell.rssi, 0.85);
          td.textContent = String(Math.round(cell.rssi));
          td.title = `${dev.name}, ${hour}:00–${hour}:59: średnio ${cell.rssi} dBm (${cell.samples} próbek)`;
        } else {
          td.classList.add("is-empty");
          td.title = `${dev.name}, ${hour}:00–${hour}:59: brak danych`;
        }
        tr.append(td);
      });
      rows.append(tr);
    }
  }

  // --- start -----------------------------------------------------------------------------------

  async function load() {
    try {
      const data = await api("GET", "/api/coverage");
      state.plan = data.plan;
      state.points = data.points;
    } catch (err) {
      say("plan-status", err.message, true);
    }
    if (state.plan) {
      const form = $("plan-form");
      form.elements.width_m.value = state.plan.width_m;
      form.elements.height_m.value = state.plan.height_m;
    }
    render();
    const running = state.points.find((p) => p.status === "measuring");
    if (running) watch(running.id);
  }

  function initTabs() {
    for (const tab of document.querySelectorAll("[data-tab]")) {
      tab.addEventListener("click", () => {
        for (const other of document.querySelectorAll("[data-tab]")) {
          const active = other === tab;
          other.classList.toggle("active", active);
          other.setAttribute("aria-selected", String(active));
        }
        for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== tab.dataset.tab;
        if (tab.dataset.tab === "survey") render();
        document.dispatchEvent(new CustomEvent("coverage:tab", { detail: tab.dataset.tab }));
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (!$("coverage-canvas")) return;
    try {
      state.bands = JSON.parse($("coverage-bands").textContent);
    } catch {
      state.bands = {};
    }
    const canvas = $("coverage-canvas");
    canvas.addEventListener("pointerdown", onPointerDown);
    canvas.addEventListener("pointermove", onPointerMove);
    canvas.addEventListener("pointerup", onPointerUp);
    canvas.addEventListener("pointercancel", () => {
      state.drag = null;
      render();
    });
    $("plan-form").addEventListener("submit", onPlanSubmit);
    const syncMode = () => canvas.classList.toggle("is-drawing", selectedMode() === "room");
    $("coverage-modes").addEventListener("change", syncMode);
    syncMode();
    $("band-filter").addEventListener("change", render);
    $("show-points").addEventListener("change", render);
    $("clear-points").addEventListener("click", clearPoints);
    let resizeTimer = null;
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(render, 150);
    });
    initTabs();
    renderLegend();
    document.addEventListener("coverage:tab", (evt) => {
      if (evt.detail === "signal") loadSignal();
    });
    $("signal-filters")?.addEventListener("change", loadSignal);
    load();
  });
})();
