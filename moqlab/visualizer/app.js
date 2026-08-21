const refreshMs = 1000;
const svg = document.querySelector("#graph");
const summary = document.querySelector("#summary");
const updated = document.querySelector("#updated");
const linksTable = document.querySelector("#links");
const nodeDetails = document.querySelector("#node-details");
const zoomMin = 0.25;
const zoomMax = 5;
let viewport;
let view = { x: 0, y: 0, scale: 1 };
let dragging = false;
let lastPointer;
let selectedNodeId = null;
let selectedLinkId = null;
let nodesById = new Map();
let linksById = new Map();
let linksEditable = false;
let routersEditable = false;
let metricsRequestNodeId = null;
let renderedNodeId = null;
let renderedLinkId = null;
let renderedLinksEditable = false;
let renderedRoutersEditable = false;
let metricFields = new Map();
let metricsStatus;
let metricsReason;
let metricsGrid;

function formatRate(bps, status) {
  if (status === "warming") return "sampling";
  if (bps === null || bps === undefined) return "N/A";
  if (bps >= 1000000) return `${(bps / 1000000).toFixed(2)} Mbps`;
  if (bps >= 1000) return `${(bps / 1000).toFixed(1)} kbps`;
  return `${bps.toFixed(0)} bps`;
}

function formatMs(value) {
  if (value === null || value === undefined) return "N/A";
  return `${Number(value).toFixed(1)} ms`;
}

function formatSampleTime(value) {
  if (value === null || value === undefined) return "N/A";
  return new Date(Number(value)).toISOString().slice(11, 23);
}

function addMetric(grid, key, label) {
  const labelEl = document.createElement("div");
  labelEl.className = "metric-label";
  labelEl.textContent = label;
  const valueEl = document.createElement("div");
  valueEl.className = "metric-value";
  valueEl.textContent = "N/A";
  metricFields.set(key, valueEl);
  grid.append(labelEl, valueEl);
}

function setMetric(key, value) {
  const field = metricFields.get(key);
  if (field) field.textContent = value;
}

function renderNodeDetails(node) {
  nodeDetails.replaceChildren();
  renderedNodeId = node?.id ?? null;
  renderedLinkId = null;
  renderedRoutersEditable = routersEditable;
  metricFields = new Map();
  metricsStatus = undefined;
  metricsReason = undefined;
  metricsGrid = undefined;
  const heading = document.createElement("h2");
  heading.textContent = "Node";
  nodeDetails.append(heading);
  if (!node) {
    const prompt = document.createElement("p");
    prompt.textContent = "Select a node or link to inspect it.";
    nodeDetails.append(prompt);
    return;
  }

  const name = document.createElement("h3");
  name.textContent = node.id;
  const identity = document.createElement("p");
  identity.textContent = [node.role, node.media_client, node.native_playback]
    .filter(Boolean).join(" · ");
  nodeDetails.append(name, identity);
  if (node.role === "router") {
    nodeDetails.append(routerAqmEditor(node));
    return;
  }
  if (node.role !== "subscriber" || node.kind !== "media") {
    const unavailable = document.createElement("p");
    unavailable.textContent = "Player metrics unavailable.";
    nodeDetails.append(unavailable);
    return;
  }

  metricsStatus = document.createElement("span");
  metricsStatus.className = "metric-status unavailable";
  metricsStatus.textContent = "loading";
  metricsReason = document.createElement("p");
  metricsReason.textContent = "Loading live metrics…";
  metricsGrid = document.createElement("div");
  metricsGrid.className = "metric-grid";
  metricsGrid.hidden = true;
  addMetric(metricsGrid, "sample", "Sample time (UTC)");
  addMetric(metricsGrid, "state", "State");
  addMetric(metricsGrid, "active", "Active quality");
  addMetric(metricsGrid, "resolution", "Resolution");
  addMetric(metricsGrid, "spatial", "Spatial layer");
  addMetric(metricsGrid, "switch", "Switch");
  addMetric(metricsGrid, "latency", "E2E latency");
  addMetric(metricsGrid, "player_rate", "Player bitrate");
  addMetric(metricsGrid, "receive_rate", "Receive bitrate");
  addMetric(metricsGrid, "catalog_rate", "Catalog bitrate");
  addMetric(metricsGrid, "buffer", "Buffer");
  addMetric(metricsGrid, "playback_rate", "Playback rate");
  addMetric(metricsGrid, "stalls", "Stalls");
  nodeDetails.append(metricsStatus, metricsReason, metricsGrid);
}

function updateNodeMetrics(payload) {
  if (!metricsStatus || !metricsReason || !metricsGrid) return;
  const status = payload.status ?? "unavailable";
  metricsStatus.className = `metric-status ${status}`;
  metricsStatus.textContent = status;
  if (!payload.metrics) {
    metricsReason.hidden = false;
    metricsReason.textContent = payload.reason ?? "Metrics unavailable.";
    metricsGrid.hidden = true;
    return;
  }

  const metrics = payload.metrics;
  const quality = metrics.quality ?? {};
  const resolution = quality.width != null && quality.height != null
    ? `${quality.width}×${quality.height}`
    : "N/A";
  metricsReason.hidden = true;
  metricsGrid.hidden = false;
  setMetric("sample", formatSampleTime(metrics.sampled_at_unix_ms));
  setMetric("state", metrics.state ?? "N/A");
  setMetric("active", metrics.active_track ?? "N/A");
  setMetric("resolution", resolution);
  setMetric("spatial", quality.spatial_id == null ? "N/A" : `S${quality.spatial_id}`);
  setMetric("switch", metrics.switch_state ?? "N/A");
  setMetric("latency", formatMs(metrics.e2e_latency_ms));
  setMetric("player_rate", formatRate(metrics.player_bitrate_bps));
  setMetric("receive_rate", formatRate(metrics.receive_bitrate_bps));
  setMetric("catalog_rate", formatRate(metrics.catalog_bitrate_bps));
  setMetric("buffer", formatMs(metrics.buffer_level_ms));
  setMetric("playback_rate", metrics.playback_rate == null ? "N/A" : `${Number(metrics.playback_rate).toFixed(2)}×`);
  setMetric("stalls", `${metrics.stall_count ?? 0} · ${formatMs(metrics.stall_duration_ms)}`);
}

async function refreshNodeMetrics() {
  const node = nodesById.get(selectedNodeId);
  if (!node || node.role !== "subscriber" || node.kind !== "media" || metricsRequestNodeId === selectedNodeId) return;
  const requestedId = selectedNodeId;
  metricsRequestNodeId = requestedId;
  try {
    const response = await fetch(`/api/nodes/${encodeURIComponent(requestedId)}/metrics`, { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    if (selectedNodeId === requestedId) updateNodeMetrics(await response.json());
  } catch (error) {
    if (selectedNodeId === requestedId) {
      updateNodeMetrics({ status: "unavailable", reason: error.message });
    }
  } finally {
    if (metricsRequestNodeId === requestedId) metricsRequestNodeId = null;
  }
}

function selectNode(node) {
  selectedNodeId = node.id;
  selectedLinkId = null;
  for (const element of document.querySelectorAll(".node")) {
    element.classList.toggle("selected", element.dataset.nodeId === selectedNodeId);
  }
  for (const element of document.querySelectorAll(".edge")) element.classList.remove("selected");
  if (renderedNodeId !== node.id) renderNodeDetails(node);
  void refreshNodeMetrics();
}

function routerAqmEditor(node) {
  const form = document.createElement("form");
  form.className = "router-form";
  const label = document.createElement("label");
  label.textContent = "AQM on all router egress";
  const select = document.createElement("select");
  select.append(new Option("None", ""), new Option("dualpi2", "dualpi2"));
  select.value = node.aqm || "";
  select.disabled = !routersEditable;
  const apply = document.createElement("button");
  apply.type = "submit";
  apply.textContent = "Apply";
  apply.disabled = !routersEditable;
  const status = document.createElement("span");
  status.className = "link-update-status";
  label.append(select);
  form.append(label, apply, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    apply.disabled = true;
    status.textContent = "Applying…";
    try {
      const response = await fetch(`/api/routers/${encodeURIComponent(node.id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ aqm: select.value || null }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      node.aqm = result.router.aqm;
      status.textContent = "Applied";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      apply.disabled = !routersEditable;
    }
  });
  return form;
}

function linkField(form, name, labelText, value, options = {}) {
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.name = name;
  input.type = "number";
  input.step = "any";
  if (options.min != null) input.min = options.min;
  if (options.max != null) input.max = options.max;
  input.value = value ?? "";
  input.disabled = !linksEditable;
  label.append(input);
  form.append(label);
}

function directionEditor(link, direction) {
  const from = direction === "forward" ? link.source : link.target;
  const to = direction === "forward" ? link.target : link.source;
  const spec = link[direction] || {};
  const form = document.createElement("form");
  form.className = `link-form ${direction}`;
  const title = document.createElement("h4");
  title.textContent = `${from} to ${to}`;
  form.append(title);
  linkField(form, "bandwidth_mbps", "Capacity (Mbps)", spec.bandwidth_mbps, { min: 0.001 });
  linkField(form, "delay_ms", "Delay (ms)", spec.delay_ms, { min: 0 });
  linkField(form, "jitter_ms", "Jitter (ms)", spec.jitter_ms, { min: 0 });
  linkField(form, "loss_pct", "Loss (%)", spec.loss_pct, { min: 0, max: 100 });

  const apply = document.createElement("button");
  apply.type = "submit";
  apply.textContent = "Apply";
  apply.disabled = !linksEditable;
  const status = document.createElement("span");
  status.className = "link-update-status";
  status.setAttribute("aria-live", "polite");
  form.append(apply, status);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    apply.disabled = true;
    status.textContent = "Applying…";
    const data = new FormData(form);
    const payload = {};
    for (const name of ["bandwidth_mbps", "delay_ms", "jitter_ms", "loss_pct"]) {
      payload[name] = data.get(name) === "" ? null : Number(data.get(name));
    }
    try {
      const response = await fetch(`/api/links/${encodeURIComponent(link.id)}/${direction}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
      link[direction] = result.direction;
      status.textContent = "Applied";
    } catch (error) {
      status.textContent = error.message;
    } finally {
      apply.disabled = !linksEditable;
    }
  });
  return form;
}

function renderLinkDetails(link) {
  nodeDetails.replaceChildren();
  renderedNodeId = null;
  renderedLinkId = link.id;
  renderedLinksEditable = linksEditable;
  const heading = document.createElement("h2");
  heading.textContent = "Physical link";
  const identity = document.createElement("p");
  identity.textContent = `${link.source} ↔ ${link.target}`;
  nodeDetails.append(heading, identity);
  if (!linksEditable) {
    const note = document.createElement("p");
    note.className = "muted";
    note.textContent = "Live editing unavailable until Containernet is running.";
    nodeDetails.append(note);
  }
  nodeDetails.append(directionEditor(link, "forward"), directionEditor(link, "reverse"));
}

function selectLink(link) {
  selectedLinkId = link.id;
  selectedNodeId = null;
  for (const element of document.querySelectorAll(".node")) element.classList.remove("selected");
  for (const element of document.querySelectorAll(".edge")) {
    element.classList.toggle("selected", element.dataset.linkId === selectedLinkId);
  }
  renderLinkDetails(link);
}

function directionText(spec, arrow) {
  if (!spec) return null;
  const parts = [];
  if (spec.bandwidth_mbps != null) parts.push(`${spec.bandwidth_mbps} Mbps cap`);
  if (spec.delay_ms != null) parts.push(`${spec.delay_ms} ms`);
  if (spec.jitter_ms != null) parts.push(`${spec.jitter_ms} ms jitter`);
  if (spec.loss_pct != null) parts.push(`${spec.loss_pct}% loss`);
  return parts.length ? `${arrow} ${parts.join(", ")}` : null;
}

function shapeText(link) {
  const parts = [
    directionText(link.forward, "→"),
    directionText(link.reverse, "←"),
  ].filter(Boolean);
  return parts.length ? parts.join(" | ") : "unshaped";
}

function layout(nodes) {
  const byLevel = new Map();
  for (const node of nodes) {
    if (!byLevel.has(node.level)) byLevel.set(node.level, []);
    byLevel.get(node.level).push(node);
  }

  const levels = [...byLevel.keys()].sort((a, b) => a - b);
  const width = Math.max(720, levels.length * 230 + 120);
  let maxRows = 1;
  for (const level of levels) {
    const group = byLevel.get(level).sort((a, b) => {
      return `${a.role}:${a.id}`.localeCompare(`${b.role}:${b.id}`);
    });
    maxRows = Math.max(maxRows, group.length);
  }

  const height = Math.max(460, maxRows * 150 + 120);
  const positions = new Map();

  levels.forEach((level, levelIndex) => {
    const group = byLevel.get(level);
    const x = 70 + levelIndex * ((width - 140) / Math.max(1, levels.length - 1));
    group.forEach((node, row) => {
      const y = group.length === 1
        ? height / 2
        : 70 + row * ((height - 140) / (group.length - 1));
      positions.set(node.id, { x, y });
    });
  });

  return { width, height, positions };
}

function draw(data) {
  nodesById = new Map(data.nodes.map((node) => [node.id, node]));
  linksById = new Map(data.links.map((link) => [link.id, link]));
  linksEditable = Boolean(data.links_editable);
  routersEditable = Boolean(data.routers_editable);
  if (selectedNodeId && !nodesById.has(selectedNodeId)) {
    selectedNodeId = null;
    renderNodeDetails(null);
  } else if (selectedNodeId && nodesById.get(selectedNodeId)?.role === "router" && renderedRoutersEditable !== routersEditable) {
    renderNodeDetails(nodesById.get(selectedNodeId));
  }
  if (selectedLinkId && !linksById.has(selectedLinkId)) {
    selectedLinkId = null;
    renderNodeDetails(null);
  } else if (selectedLinkId && (renderedLinkId !== selectedLinkId || renderedLinksEditable !== linksEditable)) {
    renderLinkDetails(linksById.get(selectedLinkId));
  }
  const { width, height, positions } = layout(data.nodes);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";

  viewport = document.createElementNS("http://www.w3.org/2000/svg", "g");
  const edgeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
  const labelLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
  const nodeLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
  viewport.append(edgeLayer, labelLayer, nodeLayer);
  svg.append(viewport);
  applyView();

  for (const link of data.links) {
    const a = positions.get(link.source);
    const b = positions.get(link.target);
    if (!a || !b) continue;

    const rate = Number(link.throughput_bps || 0);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", a.x);
    line.setAttribute("y1", a.y);
    line.setAttribute("x2", b.x);
    line.setAttribute("y2", b.y);
    line.setAttribute("class", `edge${rate > 0 ? " hot" : ""}${selectedLinkId === link.id ? " selected" : ""}`);
    line.dataset.linkId = link.id;
    line.setAttribute("stroke-width", String(Math.max(3, Math.min(12, 3 + Math.log10(rate + 1)))));
    line.addEventListener("click", (event) => { event.stopPropagation(); selectLink(link); });
    edgeLayer.append(line);
    const hit = line.cloneNode();
    hit.setAttribute("class", "edge-hit");
    hit.addEventListener("click", (event) => { event.stopPropagation(); selectLink(link); });
    edgeLayer.append(hit);

    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("x", (a.x + b.x) / 2);
    label.setAttribute("y", (a.y + b.y) / 2 - 10);
    label.setAttribute("class", "edge-label");
    label.textContent = formatRate(link.throughput_bps, link.status);
    labelLayer.append(label);
  }

  for (const node of data.nodes) {
    const p = positions.get(node.id);
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", `node ${node.role}`);
    group.classList.toggle("selected", node.id === selectedNodeId);
    group.dataset.nodeId = node.id;
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", "0");
    group.setAttribute("aria-label", `Inspect ${node.id}`);
    group.setAttribute("transform", `translate(${p.x}, ${p.y})`);
    group.addEventListener("click", () => selectNode(node));
    group.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectNode(node);
      }
    });
    const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    circle.setAttribute("r", "34");
    group.append(circle);

    const id = document.createElementNS("http://www.w3.org/2000/svg", "text");
    id.setAttribute("y", "54");
    id.textContent = node.id;
    group.append(id);

    const role = document.createElementNS("http://www.w3.org/2000/svg", "text");
    role.setAttribute("class", "role");
    role.setAttribute("y", "70");
    role.textContent = `${node.role} · ${node.media_client || "media"}`;
    group.append(role);

    nodeLayer.append(group);
  }

  linksTable.innerHTML = "";
  for (const link of data.links) {
    const tr = document.createElement("tr");
    tr.className = "link-row";
    tr.tabIndex = 0;
    tr.innerHTML = `
      <td>${link.source} &lt;-&gt; ${link.target}</td>
      <td>${formatRate(link.throughput_bps, link.status)}</td>
      <td class="muted">${shapeText(link)}</td>
    `;
    tr.addEventListener("click", () => selectLink(link));
    tr.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectLink(link);
      }
    });
    linksTable.append(tr);
  }

  const s = data.summary;
  summary.textContent = `${s.relays} relays, ${s.routers} routers, ${s.publishers} publishers, ${s.subscribers} subscribers${s.traffic_endpoints ? `, ${s.traffic_endpoints} traffic endpoints` : ""}, ${s.links} links`;
  updated.textContent = `Updated ${new Date(data.sampled_at_unix_s * 1000).toLocaleTimeString()}`;
}

function svgPoint(event) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  return point.matrixTransform(svg.getScreenCTM().inverse());
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function applyView() {
  if (!viewport) return;
  viewport.setAttribute("transform", `translate(${view.x} ${view.y}) scale(${view.scale})`);
}

function resetView() {
  view = { x: 0, y: 0, scale: 1 };
  applyView();
}

function zoomAt(event) {
  event.preventDefault();
  const mouse = svgPoint(event);
  const graphPoint = {
    x: (mouse.x - view.x) / view.scale,
    y: (mouse.y - view.y) / view.scale,
  };
  const factor = event.deltaY < 0 ? 1.15 : 1 / 1.15;
  const nextScale = clamp(view.scale * factor, zoomMin, zoomMax);
  view = {
    x: mouse.x - graphPoint.x * nextScale,
    y: mouse.y - graphPoint.y * nextScale,
    scale: nextScale,
  };
  applyView();
}

function startPan(event) {
  if (event.button !== 0) return;
  if (event.target.closest?.(".node, .edge, .edge-hit")) return;
  dragging = true;
  lastPointer = svgPoint(event);
  svg.setPointerCapture(event.pointerId);
  svg.classList.add("panning");
}

function pan(event) {
  if (!dragging) return;
  const pointer = svgPoint(event);
  view.x += pointer.x - lastPointer.x;
  view.y += pointer.y - lastPointer.y;
  lastPointer = pointer;
  applyView();
}

function endPan(event) {
  if (!dragging) return;
  dragging = false;
  lastPointer = undefined;
  svg.releasePointerCapture(event.pointerId);
  svg.classList.remove("panning");
}

svg.addEventListener("wheel", zoomAt, { passive: false });
svg.addEventListener("pointerdown", startPan);
svg.addEventListener("pointermove", pan);
svg.addEventListener("pointerup", endPan);
svg.addEventListener("pointercancel", endPan);
svg.addEventListener("dblclick", resetView);

async function refresh() {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    draw(await response.json());
    await refreshNodeMetrics();
  } catch (error) {
    updated.textContent = `Visualizer fetch failed: ${error.message}`;
  }
}

refresh();
setInterval(refresh, refreshMs);
