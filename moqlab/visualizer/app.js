const refreshMs = 1000;
const svg = document.querySelector("#graph");
const summary = document.querySelector("#summary");
const updated = document.querySelector("#updated");
const linksTable = document.querySelector("#links");
const zoomMin = 0.25;
const zoomMax = 5;
let viewport;
let view = { x: 0, y: 0, scale: 1 };
let dragging = false;
let lastPointer;

function formatRate(bps, status) {
  if (status === "warming") return "sampling";
  if (bps === null || bps === undefined) return "N/A";
  if (bps >= 1000000) return `${(bps / 1000000).toFixed(2)} Mbps`;
  if (bps >= 1000) return `${(bps / 1000).toFixed(1)} kbps`;
  return `${bps.toFixed(0)} bps`;
}

function directionText(spec, arrow) {
  if (!spec) return null;
  const parts = [];
  if (spec.bandwidth_mbps != null) parts.push(`${spec.bandwidth_mbps} Mbps cap`);
  if (spec.delay_ms != null) parts.push(`${spec.delay_ms} ms`);
  if (spec.jitter_ms != null) parts.push(`${spec.jitter_ms} ms jitter`);
  if (spec.loss_pct != null) parts.push(`${spec.loss_pct}% loss`);
  if (spec.aqm != null) parts.push(spec.aqm);
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
    line.setAttribute("class", rate > 0 ? "edge hot" : "edge");
    line.setAttribute("stroke-width", String(Math.max(3, Math.min(12, 3 + Math.log10(rate + 1)))));
    edgeLayer.append(line);

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
    group.setAttribute("transform", `translate(${p.x}, ${p.y})`);
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
    role.textContent = node.kind === "media"
      ? `${node.role} · ${node.media_client === "native" ? "native" : node.browser_mode}`
      : node.role;
    group.append(role);

    nodeLayer.append(group);
  }

  linksTable.innerHTML = "";
  for (const link of data.links) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${link.source} &lt;-&gt; ${link.target}</td>
      <td>${formatRate(link.throughput_bps, link.status)}</td>
      <td class="muted">${shapeText(link)}</td>
    `;
    linksTable.append(tr);
  }

  const s = data.summary;
  summary.textContent = `${s.relays} relays, ${s.routers} routers, ${s.publishers} publishers, ${s.subscribers} subscribers, ${s.links} links`;
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
  } catch (error) {
    updated.textContent = `Visualizer fetch failed: ${error.message}`;
  }
}

refresh();
setInterval(refresh, refreshMs);
