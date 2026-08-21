"use strict";

const svgNS = "http://www.w3.org/2000/svg";
const graph = document.querySelector("#graph");
const viewport = document.querySelector("#viewport");
const editor = document.querySelector("#editor");
const errorsEl = document.querySelector("#errors");
const validationEl = document.querySelector("#validation");
const summaryEl = document.querySelector("#summary");
const backendBadge = document.querySelector("#backend-badge");
const trafficItems = document.querySelector("#traffic-items");
const routeItems = document.querySelector("#route-items");
const linkPrompt = document.querySelector("#link-prompt");
const undoButton = document.querySelector("#undo");
const redoButton = document.querySelector("#redo");
const previewButton = document.querySelector("#preview");
const downloadButton = document.querySelector("#download");
const yamlDialog = document.querySelector("#yaml-dialog");
const yamlPreview = document.querySelector("#yaml-preview");

let schema;
let definitions;
let manifest;
let digest;
let draft = blankDraft();
let positions = {};
let selected = null;
let history = [];
let future = [];
let valid = false;
let validationTimer;
let validationSequence = 0;
let linkMode = false;
let linkStart = null;
let routeBuild = null;
let routeBuildFlow = null;
let view = { x: 0, y: 0, scale: 1 };
let panState = null;
let nodeDrag = null;
let suppressGraphClick = false;

function blankDraft() {
  return {
    topology_mode: "explicit",
    relays: {},
    publishers: {},
    subscribers: {},
    routers: {},
    links: [],
  };
}

function clone(value) {
  return structuredClone(value);
}

function snapshot() {
  return { draft: clone(draft), positions: clone(positions), selected: clone(selected) };
}

function restore(value) {
  draft = value.draft;
  positions = value.positions;
  selected = value.selected;
  renderAll();
  scheduleValidation();
  saveDraft();
}

function commit(change) {
  history.push(snapshot());
  if (history.length > 100) history.shift();
  future = [];
  change();
  renderAll();
  scheduleValidation();
  saveDraft();
}

function undo() {
  if (!history.length) return;
  future.push(snapshot());
  restore(history.pop());
}

function redo() {
  if (!future.length) return;
  history.push(snapshot());
  restore(future.pop());
}

function saveDraft() {
  if (!digest) return;
  localStorage.setItem(`moqlab-designer:${digest}`, JSON.stringify({ draft, positions }));
}

function resolveSchema(value) {
  let current = value || {};
  while (current.$ref) {
    current = definitions[current.$ref.split("/").pop()] || {};
  }
  if (current.anyOf) {
    const nonNull = current.anyOf.find((part) => part.type !== "null") || current.anyOf[0];
    return { ...resolveSchema(nonNull), default: current.default, nullable: true };
  }
  return current;
}

function schemaDefault(rawSchema) {
  const spec = resolveSchema(rawSchema);
  if (spec.default !== undefined && spec.default !== null) return clone(spec.default);
  if (spec.type === "object" || spec.properties) {
    const value = {};
    for (const [name, child] of Object.entries(spec.properties || {})) {
      const childDefault = schemaDefault(child);
      if (childDefault !== undefined) value[name] = childDefault;
    }
    return value;
  }
  return undefined;
}

function deepMerge(base, override) {
  const result = clone(base || {});
  for (const [name, value] of Object.entries(override || {})) {
    if (value && typeof value === "object" && !Array.isArray(value)
        && result[name] && typeof result[name] === "object" && !Array.isArray(result[name])) {
      result[name] = deepMerge(result[name], value);
    } else {
      result[name] = clone(value);
    }
  }
  return result;
}

function effectiveDefaults() {
  return deepMerge(schemaDefault(schema.properties.defaults), draft.defaults || {});
}

function inheritedNodeValues(role, config) {
  const defaults = effectiveDefaults();
  if (role === "relay") return defaults.relay;
  if (role === "router") return defaults.router;
  if (role === "publisher") {
    return {
      image: defaults.publisher.image,
    };
  }
  if (role === "subscriber") {
    const client = config.media_client || defaults.subscriber.media_client;
    return {
      image: client === "native" ? defaults.subscriber.native_media_image : defaults.subscriber.image,
      log_level: defaults.subscriber.log_level,
      media_client: defaults.subscriber.media_client,
      native_playback: defaults.subscriber.native_playback,
      minimal_buffer_ms: 200,
      target_latency_ms: 300,
    };
  }
  if (role.startsWith("traffic-")) return { image: defaults.traffic.image };
  return {};
}

function fieldTitle(name, fieldSchema) {
  return fieldSchema.title || name.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function renderObjectEditor(container, rawSchema, value, onReplace, options = {}) {
  const objectSchema = resolveSchema(rawSchema);
  const required = new Set(objectSchema.required || []);
  const common = document.createElement("div");
  const advanced = document.createElement("details");
  const advancedSummary = document.createElement("summary");
  advancedSummary.textContent = "Advanced attributes";
  advanced.append(advancedSummary);
  let advancedCount = 0;

  for (const [name, rawFieldSchema] of Object.entries(objectSchema.properties || {})) {
    if ((options.skip || []).includes(name)) continue;
    const fieldSchema = resolveSchema(rawFieldSchema);
    const fieldRequired = required.has(name) || options.alwaysSet?.includes(name);
    const target = fieldRequired || (fieldSchema.default !== undefined && fieldSchema.default !== null) || options.common?.includes(name)
      ? common
      : advanced;
    if (target === advanced) advancedCount += 1;
    target.append(renderField(name, rawFieldSchema, value?.[name], fieldRequired, (next, unset) => {
      const updated = clone(value || {});
      if (unset) delete updated[name];
      else updated[name] = next;
      onReplace(updated);
    }, options.inherited?.[name]));
  }
  container.append(common);
  if (advancedCount) container.append(advanced);
}

function renderField(name, rawSchema, current, required, onChange, inherited) {
  const spec = resolveSchema(rawSchema);
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  const label = document.createElement("label");
  label.textContent = `${name === "media_client" ? "Mode" : fieldTitle(name, spec)}${required ? " *" : ""}`;
  wrapper.append(label);

  if (spec.type === "object" || spec.properties) {
    const fieldset = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = fieldTitle(name, spec);
    fieldset.append(legend);
    const enabled = current !== undefined && current !== null;
    if (!required) {
      const toggleLabel = document.createElement("label");
      toggleLabel.className = "inline";
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = enabled;
      toggle.addEventListener("change", () => onChange(toggle.checked ? {} : undefined, !toggle.checked));
      const inheritedText = inherited === undefined ? "" : `: ${JSON.stringify(inherited)}`;
      toggleLabel.append(toggle, document.createTextNode(`Override${enabled ? "" : ` inherited${inheritedText}`}`));
      fieldset.append(toggleLabel);
    }
    if (required || enabled) {
      renderObjectEditor(fieldset, spec, current || {}, (next) => onChange(next, false), { inherited });
    }
    wrapper.replaceChildren(fieldset);
    return wrapper;
  }

  if (spec.type === "array") {
    const input = document.createElement("textarea");
    input.rows = 2;
    input.value = Array.isArray(current) ? current.join(", ") : Array.isArray(inherited) ? inherited.join(", ") : "";
    input.placeholder = "Comma-separated values";
    input.addEventListener("change", () => {
      if (!input.value.trim() && !required) return onChange(undefined, true);
      const itemSpec = resolveSchema(spec.items || {});
      const items = input.value.split(",").map((item) => item.trim()).filter(Boolean).map((item) => {
        return itemSpec.type === "number" ? Number(item) : itemSpec.type === "integer" ? Number.parseInt(item, 10) : item;
      });
      onChange(items, false);
    });
    wrapper.append(input);
    return wrapper;
  }

  if (spec.type === "boolean") {
    const select = document.createElement("select");
    if (!required) select.append(new Option(inherited === undefined ? "Inherit / unset" : `Inherit (${inherited})`, ""));
    select.append(new Option("true", "true"), new Option("false", "false"));
    select.value = current === undefined || current === null ? "" : String(current);
    select.addEventListener("change", () => {
      if (select.value === "") onChange(undefined, true);
      else onChange(select.value === "true", false);
    });
    wrapper.append(select);
    return wrapper;
  }

  if (spec.enum || spec.const !== undefined) {
    const select = document.createElement("select");
    if (!required && spec.const === undefined) select.append(new Option(inherited === undefined ? "Inherit / unset" : `Inherit (${inherited})`, ""));
    const values = spec.enum || [spec.const];
    const labels = {
      "chrome-headless": "Chrome headless",
      chrome: "Chrome",
      native: "Native",
      receive: "Receive",
      simulate: "Simulate",
    };
    for (const item of values) select.append(new Option(labels[item] || String(item), String(item)));
    select.value = current ?? inherited ?? spec.default ?? "";
    select.addEventListener("change", () => {
      if (select.value === "") onChange(undefined, true);
      else onChange(select.value, false);
    });
    wrapper.append(select);
    return wrapper;
  }

  const input = document.createElement("input");
  input.type = spec.type === "integer" || spec.type === "number" ? "number" : "text";
  if (spec.type === "number") input.step = "any";
  if (spec.minimum !== undefined) input.min = spec.minimum;
  if (spec.exclusiveMinimum !== undefined) input.min = spec.exclusiveMinimum;
  if (spec.maximum !== undefined) input.max = spec.maximum;
  if (spec.minLength !== undefined) input.minLength = spec.minLength;
  if (spec.maxLength !== undefined) input.maxLength = spec.maxLength;
  if (spec.pattern) input.pattern = spec.pattern;
  input.value = current ?? inherited ?? "";
  if (current === undefined && inherited !== undefined) input.title = `Inherited value: ${inherited}`;
  else if (spec.default !== undefined) input.placeholder = `Default: ${spec.default}`;
  input.required = required;
  input.addEventListener("change", () => {
    if (input.value === "" && !required) return onChange(undefined, true);
    const next = spec.type === "number" ? Number(input.value)
      : spec.type === "integer" ? Number.parseInt(input.value, 10)
      : input.value;
    onChange(next, false);
  });
  wrapper.append(input);
  if (current === undefined && inherited !== undefined) {
    const help = document.createElement("small");
    help.textContent = "Inherited default; exported only when changed.";
    wrapper.append(help);
  }
  if (spec.description) {
    const help = document.createElement("small");
    help.textContent = spec.description;
    wrapper.append(help);
  }
  return wrapper;
}

function allNodes() {
  const nodes = [];
  for (const [role, property] of [["relay", "relays"], ["router", "routers"], ["publisher", "publishers"], ["subscriber", "subscribers"]]) {
    for (const id of Object.keys(draft[property] || {})) nodes.push({ id, role });
  }
  for (const endpoint of ["sender", "receiver"]) {
    const value = draft.traffic?.[endpoint];
    if (value?.id) nodes.push({ id: value.id, role: `traffic-${endpoint}` });
  }
  return nodes;
}

function nodeExists(id) {
  return allNodes().some((node) => node.id === id);
}

function nextId(prefix) {
  if (!nodeExists(prefix)) return prefix;
  let index = 1;
  while (nodeExists(`${prefix}-${index}`)) index += 1;
  return `${prefix}-${index}`;
}

function portEntries() {
  const entries = [];
  const add = (path, value) => {
    if (value !== undefined && value !== null && value !== "") entries.push({ path, value: Number(value) });
  };
  for (const [id, relay] of Object.entries(draft.relays || {})) {
    add(`relays.${id}.listen_port`, relay.listen_port);
    add(`relays.${id}.admin_port`, relay.admin_port);
  }
  for (const [id, publisher] of Object.entries(draft.publishers || {})) {
    add(`publishers.${id}.listen_port`, publisher.listen_port);
    add(`publishers.${id}.fingerprint_port`, publisher.fingerprint_port);
  }
  if (draft.traffic) {
    const traffic = effectiveDefaults().traffic;
    add("defaults.traffic.tcp_port", traffic.tcp_port);
    add("defaults.traffic.udp_port", traffic.udp_port);
  }
  return entries;
}

function portCollisionErrors() {
  const byPort = new Map();
  for (const entry of portEntries()) {
    if (!byPort.has(entry.value)) byPort.set(entry.value, []);
    byPort.get(entry.value).push(entry.path);
  }
  const errors = [];
  for (const [port, entries] of byPort) {
    if (entries.length > 1) {
      errors.push({ loc: entries, message: `port ${port} collides across ${entries.join(", ")}` });
    }
  }
  return errors;
}

function allocatePort(preferred) {
  const used = new Set(portEntries().map((entry) => entry.value));
  let port = preferred;
  while (used.has(port) && port <= 65535) port += 1;
  if (port > 65535) throw new Error("No free service port remains.");
  return port;
}

function availableTrafficDefaults() {
  const current = effectiveDefaults().traffic;
  const used = new Set(portEntries().map((entry) => entry.value));
  const next = (preferred) => {
    let port = preferred;
    while (used.has(port) && port <= 65535) port += 1;
    if (port > 65535) throw new Error("No free traffic port remains.");
    used.add(port);
    return port;
  };
  return { image: current.image, tcp_port: next(current.tcp_port), udp_port: next(current.udp_port) };
}

function nextRelayPorts() {
  const used = new Set(portEntries().map((entry) => entry.value));
  let listen = 9668;
  while (used.has(listen) || used.has(listen + 1)) listen += 2;
  return [listen, listen + 1];
}

function firstRelay() {
  return Object.keys(draft.relays || {})[0] || "";
}

function nodeDefaults(role) {
  const defaults = effectiveDefaults();
  if (role === "relay") {
    const [listen_port, admin_port] = nextRelayPorts();
    return {
      id: nextId("relay"),
      config: {
        listen_port,
        admin_port,
        upstream: null,
        image: defaults.relay.image,
        endpoint: defaults.relay.endpoint,
        tls: clone(defaults.relay.tls),
        cache: clone(defaults.relay.cache),
      },
    };
  }
  if (role === "router") return { id: nextId("router"), config: { image: defaults.router.image } };
  if (role === "publisher") {
    return {
      id: nextId("pub"),
      config: {
        kind: "media",
        connects_to: firstRelay(),
        asset: "testsvc",
        listen_port: allocatePort(4443),
        fingerprint_port: allocatePort(8081),
        image: defaults.publisher.image,
      },
    };
  }
  if (role === "subscriber") {
    return {
      id: nextId("sub"),
      config: {
        kind: "media",
        connects_to: firstRelay(),
        namespace: "msf/clear",
        track: "video/s2",
        image: defaults.subscriber.image,
        log_level: defaults.subscriber.log_level,
        media_client: defaults.subscriber.media_client,
      },
    };
  }
  if (role === "traffic-sender") return { id: nextId("traffic-tx"), config: { image: defaults.traffic.image } };
  return { id: nextId("traffic-rx"), config: { image: defaults.traffic.image } };
}

function addNode(role, point) {
  const endpoint = role.startsWith("traffic-") ? role.slice(8) : null;
  if (endpoint && draft.traffic?.[endpoint]) {
    alert(`Traffic ${endpoint} already exists.`);
    return;
  }
  const trafficDefaults = endpoint && !draft.traffic ? availableTrafficDefaults() : null;
  const value = nodeDefaults(role);
  commit(() => {
    if (role === "publisher" || role === "subscriber") ensureMediaTls();
    if (endpoint) {
      if (trafficDefaults) {
        draft.defaults ||= {};
        draft.defaults.traffic = trafficDefaults;
      }
      draft.traffic ||= { routes: {}, flows: [] };
      draft.traffic[endpoint] = { id: value.id };
    } else {
      const property = `${role}s`;
      draft[property] ||= {};
      draft[property][value.id] = value.config;
    }
    positions[value.id] = point || nextCanvasPosition();
    selected = { kind: "node", role, id: value.id };
  });
}

function nextCanvasPosition() {
  const count = allNodes().length;
  return { x: 130 + (count % 5) * 190, y: 120 + Math.floor(count / 5) * 150 };
}

function propertyForRole(role) {
  return `${role}s`;
}

function nodeConfig(role, id) {
  if (role.startsWith("traffic-")) return draft.traffic?.[role.slice(8)];
  return draft[propertyForRole(role)]?.[id];
}

function ensureMediaTls() {
  draft.defaults ||= {};
  draft.defaults.relay ||= {};
  draft.defaults.relay.tls = { insecure: false, generated: true };
  for (const relay of Object.values(draft.relays || {})) {
    relay.tls = { insecure: false, generated: true };
  }
}

function normalizedNodeConfig(role, next) {
  const value = clone(next);
  const defaults = effectiveDefaults();
  if (role === "subscriber") {
    const client = value.media_client || defaults.subscriber.media_client;
    value.image = client === "native" ? defaults.subscriber.native_media_image : defaults.subscriber.image;
    if (client === "native") {
      const playback = value.native_playback || defaults.subscriber.native_playback;
      value.native_playback = playback;
      if (playback === "simulate") {
        value.minimal_buffer_ms ??= 200;
        value.target_latency_ms ??= 300;
      } else {
        delete value.minimal_buffer_ms;
        delete value.target_latency_ms;
      }
    } else {
      delete value.native_playback;
      value.minimal_buffer_ms ??= 200;
      value.target_latency_ms ??= 300;
    }
  }
  return value;
}

function cleanSubscriberModeFields() {
  const defaults = effectiveDefaults().subscriber;
  for (const subscriber of Object.values(draft.subscribers || {})) {
    delete subscriber.browser_mode;
    const client = subscriber.media_client || defaults.media_client;
    if (client !== "native") {
      delete subscriber.native_playback;
    } else if ((subscriber.native_playback || defaults.native_playback) !== "simulate") {
      delete subscriber.minimal_buffer_ms;
      delete subscriber.target_latency_ms;
    }
  }
}

function nodeDefinition(role) {
  if (role.startsWith("traffic-")) return definitions.TrafficEndpointConfig;
  return definitions[manifest.nodeCollections[role].definition];
}

function renameNode(role, oldId, newId) {
  newId = newId.trim();
  if (!newId || (newId !== oldId && nodeExists(newId))) {
    alert("Node id must be non-empty and globally unique.");
    renderInspector();
    return;
  }
  if (newId === oldId) return;
  commit(() => {
    if (role.startsWith("traffic-")) {
      draft.traffic[role.slice(8)].id = newId;
    } else {
      const property = propertyForRole(role);
      const replaced = {};
      for (const [id, value] of Object.entries(draft[property])) replaced[id === oldId ? newId : id] = value;
      draft[property] = replaced;
    }
    for (const relay of Object.values(draft.relays || {})) if (relay.upstream === oldId) relay.upstream = newId;
    for (const item of [...Object.values(draft.publishers || {}), ...Object.values(draft.subscribers || {})]) {
      if (item.connects_to === oldId) item.connects_to = newId;
    }
    for (const link of draft.links || []) {
      if (link.from === oldId) link.from = newId;
      if (link.to === oldId) link.to = newId;
    }
    for (const route of Object.values(draft.traffic?.routes || {})) {
      route.path = route.path.map((id) => id === oldId ? newId : id);
    }
    positions[newId] = positions[oldId] || nextCanvasPosition();
    delete positions[oldId];
    selected.id = newId;
  });
}

function nodeReferences(id) {
  const refs = [];
  for (const [rid, relay] of Object.entries(draft.relays || {})) if (relay.upstream === id) refs.push(`relay ${rid} upstream`);
  for (const [pid, pub] of Object.entries(draft.publishers || {})) if (pub.connects_to === id) refs.push(`publisher ${pid}`);
  for (const [sid, sub] of Object.entries(draft.subscribers || {})) if (sub.connects_to === id) refs.push(`subscriber ${sid}`);
  for (const [name, route] of Object.entries(draft.traffic?.routes || {})) if (route.path.includes(id)) refs.push(`traffic route ${name}`);
  return refs;
}

function deleteNode(role, id) {
  const refs = nodeReferences(id);
  if (refs.length) {
    alert(`Cannot delete ${id}; referenced by ${refs.join(", ")}. Rewire first.`);
    return;
  }
  const attached = (draft.links || []).filter((link) => link.from === id || link.to === id).length;
  if (!confirm(`Delete ${id}${attached ? ` and ${attached} attached link(s)` : ""}?`)) return;
  commit(() => {
    if (role.startsWith("traffic-")) delete draft.traffic[role.slice(8)];
    else delete draft[propertyForRole(role)][id];
    if (draft.traffic && !draft.traffic.sender && !draft.traffic.receiver
        && !Object.keys(draft.traffic.routes || {}).length && !(draft.traffic.flows || []).length) {
      delete draft.traffic;
    }
    draft.links = (draft.links || []).filter((link) => link.from !== id && link.to !== id);
    delete positions[id];
    selected = null;
  });
}

function duplicateNodes(role, id, count) {
  if (role.startsWith("traffic-")) return alert("Traffic endpoints are singletons.");
  const source = clone(nodeConfig(role, id));
  const copyLinks = ["publisher", "subscriber"].includes(role) && confirm("Copy incident physical links too?");
  commit(() => {
    for (let index = 0; index < count; index += 1) {
      const newId = nextId(id.replace(/-\d+$/, ""));
      const config = clone(source);
      if (role === "relay") {
        const [listen_port, admin_port] = nextRelayPorts();
        config.listen_port = listen_port;
        config.admin_port = admin_port;
      } else if (role === "publisher") {
        config.listen_port = allocatePort(config.listen_port || 4443);
        config.fingerprint_port = allocatePort(config.fingerprint_port || 8081);
        if (config.fingerprint_port === config.listen_port) config.fingerprint_port = allocatePort(config.fingerprint_port + 1);
      } else if (role === "publisher" && config.port !== undefined) {
        config.port = allocatePort(config.port);
      }
      draft[propertyForRole(role)][newId] = config;
      const origin = positions[id] || nextCanvasPosition();
      positions[newId] = { x: origin.x + 45 * (index + 1), y: origin.y + 45 * (index + 1) };
      if (copyLinks) {
        for (const link of [...draft.links]) {
          if (link.from !== id && link.to !== id) continue;
          const copied = clone(link);
          if (copied.from === id) copied.from = newId;
          if (copied.to === id) copied.to = newId;
          draft.links.push(copied);
        }
      }
      selected = { kind: "node", role, id: newId };
    }
  });
}

function canonicalPair(a, b) {
  return [a, b].sort().join("\0");
}

function addPhysicalLink(a, b) {
  if (a === b) { alert("Link endpoints must differ."); return false; }
  if ((draft.links || []).some((link) => canonicalPair(link.from, link.to) === canonicalPair(a, b))) {
    alert("Physical link already exists.");
    return false;
  }
  commit(() => {
    draft.links ||= [];
    draft.links.push({ from: a, to: b });
    selected = { kind: "link", index: draft.links.length - 1 };
    linkStart = null;
  });
  return true;
}

function linkUsedByRoute(link) {
  const wanted = canonicalPair(link.from, link.to);
  for (const [name, route] of Object.entries(draft.traffic?.routes || {})) {
    for (let index = 0; index + 1 < route.path.length; index += 1) {
      if (canonicalPair(route.path[index], route.path[index + 1]) === wanted) return name;
    }
  }
  return null;
}

function deleteLink(index) {
  const route = linkUsedByRoute(draft.links[index]);
  if (route) return alert(`Link is used by traffic route ${route}. Change route first.`);
  if (!confirm("Delete physical link?")) return;
  commit(() => {
    draft.links.splice(index, 1);
    selected = null;
  });
}

function shortestTrafficPath() {
  const sender = draft.traffic?.sender?.id;
  const receiver = draft.traffic?.receiver?.id;
  if (!sender || !receiver) return null;
  const allowed = new Set([sender, receiver, ...Object.keys(draft.routers || {})]);
  const adjacency = new Map();
  for (const link of draft.links || []) {
    if (!allowed.has(link.from) || !allowed.has(link.to)) continue;
    if (!adjacency.has(link.from)) adjacency.set(link.from, []);
    if (!adjacency.has(link.to)) adjacency.set(link.to, []);
    adjacency.get(link.from).push(link.to);
    adjacency.get(link.to).push(link.from);
  }
  const queue = [[sender]];
  const seen = new Set([sender]);
  while (queue.length) {
    const path = queue.shift();
    const last = path[path.length - 1];
    if (last === receiver) return path.length >= 3 ? path : null;
    for (const next of (adjacency.get(last) || []).sort()) {
      if (!seen.has(next)) {
        seen.add(next);
        queue.push([...path, next]);
      }
    }
  }
  return null;
}

function appendRouteNode(id) {
  const route = draft.traffic?.routes?.[routeBuild];
  if (!route) { routeBuild = null; routeBuildFlow = null; return; }
  const returnSelection = routeBuildFlow === null
    ? { kind: "route", name: routeBuild }
    : { kind: "flow", index: routeBuildFlow };
  const sender = draft.traffic.sender.id;
  const receiver = draft.traffic.receiver.id;
  if (id === sender) {
    commit(() => { route.path = [sender]; selected = returnSelection; });
    linkPrompt.textContent = "Route reset. Select connected router.";
    return;
  }
  const last = route.path[route.path.length - 1];
  const linked = (draft.links || []).some((link) => canonicalPair(link.from, link.to) === canonicalPair(last, id));
  if (!linked) return alert(`${id} is not physically linked to ${last}.`);
  if (route.path.includes(id)) return alert("Traffic route cannot repeat a node.");
  if (id !== receiver && !(id in (draft.routers || {}))) return alert("Intermediate traffic nodes must be routers.");
  if (id === receiver && route.path.length < 2) return alert("Route needs at least one router.");
  commit(() => { route.path.push(id); selected = returnSelection; });
  if (id === receiver) {
    routeBuild = null;
    routeBuildFlow = null;
    linkPrompt.textContent = "Traffic route complete.";
  } else {
    linkPrompt.textContent = `Route ends at ${id}. Select connected router or receiver.`;
  }
}

function addRoute() {
  if (!draft.traffic?.sender || !draft.traffic?.receiver) return alert("Add traffic sender and receiver first.");
  let name = "route";
  let index = 1;
  while (draft.traffic.routes?.[name]) name = `route-${index++}`;
  const path = shortestTrafficPath() || [draft.traffic.sender.id, draft.traffic.receiver.id];
  commit(() => {
    draft.traffic.routes ||= {};
    draft.traffic.routes[name] = { path };
    selected = { kind: "route", name };
  });
}

function renameRoute(oldName, newName) {
  newName = newName.trim();
  if (!newName || (newName !== oldName && draft.traffic.routes[newName])) return alert("Route name must be unique.");
  if (newName === oldName) return;
  commit(() => {
    const routes = {};
    for (const [name, route] of Object.entries(draft.traffic.routes)) routes[name === oldName ? newName : name] = route;
    draft.traffic.routes = routes;
    for (const flow of draft.traffic.flows || []) if (flow.route === oldName) flow.route = newName;
    selected.name = newName;
  });
}

function deleteRoute(name) {
  const flow = (draft.traffic?.flows || []).find((item) => item.route === name);
  if (flow) return alert(`Route used by flow ${flow.id}. Change or delete flow first.`);
  if (!confirm(`Delete route ${name}?`)) return;
  commit(() => {
    delete draft.traffic.routes[name];
    selected = null;
  });
}

function addFlow() {
  const route = Object.keys(draft.traffic?.routes || {})[0];
  if (!route) return alert("Add traffic route first.");
  const used = new Set((draft.traffic.flows || []).map((flow) => flow.id));
  let id = "flow";
  let index = 1;
  while (used.has(id)) id = `flow-${index++}`;
  commit(() => {
    draft.traffic.flows ||= [];
    draft.traffic.flows.push({ id, kind: "bulk", route, duration_s: 30 });
    selected = { kind: "flow", index: draft.traffic.flows.length - 1 };
  });
}

function addTrafficLoad() {
  if (!draft.traffic?.sender || !draft.traffic?.receiver) {
    return alert("Add traffic sender and receiver first.");
  }
  const path = shortestTrafficPath();
  if (!path) {
    return alert("Connect sender to receiver through at least one router first.");
  }
  const usedFlows = new Set((draft.traffic.flows || []).map((flow) => flow.id));
  let number = 1;
  while (usedFlows.has(`load-${number}`) || draft.traffic.routes?.[`load-${number}-route`]) number += 1;
  const id = `load-${number}`;
  const route = `${id}-route`;
  commit(() => {
    draft.traffic.routes ||= {};
    draft.traffic.flows ||= [];
    draft.traffic.routes[route] = { path };
    draft.traffic.flows.push({ id, kind: "cbr", route, duration_s: 30, rate_mbps: 10 });
    selected = { kind: "flow", index: draft.traffic.flows.length - 1 };
  });
}

function autoLayout(pushHistory = true) {
  const action = () => {
    const groups = { publisher: 0, "traffic-sender": 0, relay: 1, router: 2, subscriber: 3, "traffic-receiver": 3 };
    const rows = new Map();
    for (const node of allNodes()) {
      const column = groups[node.role] ?? 1;
      const row = rows.get(column) || 0;
      positions[node.id] = { x: 130 + column * 240, y: 110 + row * 130 };
      rows.set(column, row + 1);
    }
  };
  if (pushHistory) commit(action);
  else action();
}

function renderGraph() {
  viewport.replaceChildren();
  for (const node of allNodes()) if (!positions[node.id]) positions[node.id] = nextCanvasPosition();
  const nodeMap = new Map(allNodes().map((node) => [node.id, node]));

  const applicationEdges = [];
  for (const [id, relay] of Object.entries(draft.relays || {})) if (relay.upstream) applicationEdges.push([id, relay.upstream]);
  for (const [id, item] of Object.entries(draft.publishers || {})) if (item.connects_to) applicationEdges.push([id, item.connects_to]);
  for (const [id, item] of Object.entries(draft.subscribers || {})) if (item.connects_to) applicationEdges.push([id, item.connects_to]);
  for (const [a, b] of applicationEdges) drawLine(a, b, "edge application");

  (draft.links || []).forEach((link, index) => {
    drawLine(link.from, link.to, `edge${selected?.kind === "link" && selected.index === index ? " selected" : ""}`, () => {
      selected = { kind: "link", index };
      renderAll();
    });
  });

  if (selected?.kind === "route") {
    const path = draft.traffic?.routes?.[selected.name]?.path || [];
    for (let index = 0; index + 1 < path.length; index += 1) drawLine(path[index], path[index + 1], "edge route");
  } else if (selected?.kind === "flow") {
    const flow = draft.traffic?.flows?.[selected.index];
    const path = draft.traffic?.routes?.[flow?.route]?.path || [];
    for (let index = 0; index + 1 < path.length; index += 1) drawLine(path[index], path[index + 1], "edge route");
  }

  for (const node of nodeMap.values()) drawNode(node);
  viewport.setAttribute("transform", `translate(${view.x} ${view.y}) scale(${view.scale})`);
}

function drawLine(aId, bId, className, onSelect) {
  const a = positions[aId];
  const b = positions[bId];
  if (!a || !b) return;
  if (onSelect) {
    const hit = document.createElementNS(svgNS, "line");
    setLine(hit, a, b);
    hit.setAttribute("class", "edge-hit");
    hit.addEventListener("click", (event) => { event.stopPropagation(); onSelect(); });
    viewport.append(hit);
  }
  const line = document.createElementNS(svgNS, "line");
  setLine(line, a, b);
  line.setAttribute("class", className);
  if (onSelect) line.addEventListener("click", (event) => { event.stopPropagation(); onSelect(); });
  viewport.append(line);
}

function setLine(line, a, b) {
  line.setAttribute("x1", a.x);
  line.setAttribute("y1", a.y);
  line.setAttribute("x2", b.x);
  line.setAttribute("y2", b.y);
}

function drawNode(node) {
  const point = positions[node.id];
  const group = document.createElementNS(svgNS, "g");
  group.setAttribute("class", `node ${node.role}${selected?.kind === "node" && selected.id === node.id ? " selected" : ""}`);
  group.setAttribute("transform", `translate(${point.x} ${point.y})`);
  group.setAttribute("tabindex", "0");
  group.setAttribute("role", "button");
  group.setAttribute("aria-label", `${node.role} ${node.id}`);
  group.dataset.nodeId = node.id;
  group.addEventListener("click", (event) => {
    event.stopPropagation();
    suppressGraphClick = false;
    if (routeBuild) {
      appendRouteNode(node.id);
      return;
    }
    if (linkMode) {
      if (!linkStart) {
        linkStart = node.id;
        linkPrompt.textContent = `First endpoint: ${linkStart}. Select second node.`;
      } else if (linkStart !== node.id) {
        if (addPhysicalLink(linkStart, node.id)) {
          linkMode = false;
          linkPrompt.textContent = "Physical link added.";
        }
      }
      return;
    }
    selected = { kind: "node", role: node.role, id: node.id };
    renderAll();
  });
  group.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      group.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    }
  });
  group.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    event.stopPropagation();
    routeBuild = null;
    routeBuildFlow = null;
    linkMode = true;
    linkStart = node.id;
    selected = { kind: "node", role: node.role, id: node.id };
    linkPrompt.textContent = `Connect ${node.id} to another node.`;
    renderAll();
  });
  group.addEventListener("pointerdown", (event) => startNodeDrag(event, node));
  const circle = document.createElementNS(svgNS, "circle");
  circle.setAttribute("r", "34");
  const id = document.createElementNS(svgNS, "text");
  id.setAttribute("y", "53");
  id.textContent = node.id;
  const role = document.createElementNS(svgNS, "text");
  role.setAttribute("class", "role");
  role.setAttribute("y", "69");
  role.textContent = node.role;
  group.append(circle, id, role);
  viewport.append(group);
}

function graphPoint(event) {
  const rect = graph.getBoundingClientRect();
  return {
    x: (event.clientX - rect.left - view.x) / view.scale,
    y: (event.clientY - rect.top - view.y) / view.scale,
  };
}

function startNodeDrag(event, node) {
  if (event.button !== 0 || linkMode || routeBuild) return;
  event.stopPropagation();
  suppressGraphClick = true;
  selected = { kind: "node", role: node.role, id: node.id };
  renderInspector();
  for (const element of document.querySelectorAll(".node")) {
    element.classList.toggle("selected", element.dataset.nodeId === node.id);
  }
  history.push(snapshot());
  if (history.length > 100) history.shift();
  future = [];
  nodeDrag = { id: node.id, pointerId: event.pointerId };
  graph.setPointerCapture(event.pointerId);
}

graph.addEventListener("pointermove", (event) => {
  if (nodeDrag) {
    positions[nodeDrag.id] = graphPoint(event);
    renderGraph();
  } else if (panState) {
    view.x += event.clientX - panState.x;
    view.y += event.clientY - panState.y;
    panState = { x: event.clientX, y: event.clientY };
    renderGraph();
  }
});

graph.addEventListener("pointerup", (event) => {
  if (nodeDrag) {
    nodeDrag = null;
    saveDraft();
    updateHistoryButtons();
  }
  panState = null;
  graph.classList.remove("panning");
  if (graph.hasPointerCapture(event.pointerId)) graph.releasePointerCapture(event.pointerId);
});

graph.addEventListener("pointerdown", (event) => {
  if (event.target.closest?.(".node, .edge, .edge-hit") || event.button !== 0) return;
  panState = { x: event.clientX, y: event.clientY };
  graph.setPointerCapture(event.pointerId);
  graph.classList.add("panning");
});

graph.addEventListener("wheel", (event) => {
  event.preventDefault();
  view.scale = Math.max(.3, Math.min(3, view.scale * (event.deltaY < 0 ? 1.12 : 1 / 1.12)));
  renderGraph();
}, { passive: false });

graph.addEventListener("dragover", (event) => event.preventDefault());
graph.addEventListener("drop", (event) => {
  event.preventDefault();
  const role = event.dataTransfer.getData("text/moqlab-role");
  if (role) addNode(role, graphPoint(event));
});

graph.addEventListener("click", () => {
  if (suppressGraphClick) {
    suppressGraphClick = false;
    return;
  }
  if (routeBuild) {
    routeBuild = null;
    routeBuildFlow = null;
    linkPrompt.textContent = "Route building cancelled.";
  } else if (linkMode) {
    linkMode = false;
    linkStart = null;
    linkPrompt.textContent = "Link creation cancelled.";
  } else {
    selected = null;
    renderAll();
  }
});

function renderInspector() {
  editor.replaceChildren();
  if (!selected) {
    const title = document.createElement("h2");
    title.textContent = "Inspector";
    const text = document.createElement("p");
    text.textContent = "Select node, link, route, flow, defaults, or startup.";
    editor.append(title, text);
    return;
  }
  if (selected.kind === "global") return renderGlobalInspector();
  if (selected.kind === "node") return renderNodeInspector();
  if (selected.kind === "link") return renderLinkInspector();
  if (selected.kind === "route") return renderRouteInspector();
  if (selected.kind === "flow") return renderFlowInspector();
}

function heading(text) {
  const title = document.createElement("h2");
  title.textContent = text;
  editor.append(title);
}

function idField(labelText, value, onChange) {
  const wrapper = document.createElement("div");
  wrapper.className = "field";
  const label = document.createElement("label");
  label.textContent = labelText;
  const input = document.createElement("input");
  input.value = value;
  input.required = true;
  input.addEventListener("change", () => onChange(input.value));
  wrapper.append(label, input);
  editor.append(wrapper);
}

function renderNodeInspector() {
  const { role, id } = selected;
  const config = nodeConfig(role, id);
  if (!config) { selected = null; return renderInspector(); }
  heading(`${role} node`);
  idField("Node id", id, (next) => renameNode(role, id, next));
  const relationship = manifest.relationships[role];
  if (relationship) {
    const wrapper = document.createElement("div");
    wrapper.className = "field";
    const label = document.createElement("label");
    label.textContent = fieldTitle(relationship, {});
    const select = document.createElement("select");
    if (role === "relay") select.append(new Option("Origin relay (no upstream)", ""));
    else select.append(new Option("Select relay", ""));
    for (const relayId of Object.keys(draft.relays || {})) {
      if (relayId !== id) select.append(new Option(relayId, relayId));
    }
    select.value = config[relationship] || "";
    select.addEventListener("change", () => commit(() => {
      if (select.value) config[relationship] = select.value;
      else if (role === "relay") config[relationship] = null;
      else config[relationship] = "";
    }));
    wrapper.append(label, select);
    editor.append(wrapper);
  }
  const nativeSubscriber = role === "subscriber"
    && (config.media_client || effectiveDefaults().subscriber.media_client) === "native";
  const simulatedNative = nativeSubscriber
    && (config.native_playback || effectiveDefaults().subscriber.native_playback) === "simulate";
  const bufferedSubscriber = role === "subscriber" && (!nativeSubscriber || simulatedNative);
  const commonFields = role === "publisher"
    ? ["asset", "listen_port", "fingerprint_port"]
    : role === "subscriber"
      ? ["namespace", "track", "media_client", ...(nativeSubscriber ? ["native_playback"] : []), ...(bufferedSubscriber ? ["minimal_buffer_ms", "target_latency_ms"] : [])]
      : role === "router" ? ["aqm"] : ["kind", "namespace", "track"];
  renderObjectEditor(editor, nodeDefinition(role), config, (next) => commit(() => {
    if (role.startsWith("traffic-")) draft.traffic[role.slice(8)] = next;
    else draft[propertyForRole(role)][id] = normalizedNodeConfig(role, next);
  }), {
    skip: [
      ...(["publisher", "subscriber"].includes(role) ? ["kind"] : []),
      ...(role === "subscriber" && !nativeSubscriber ? ["native_playback"] : []),
      ...(role === "subscriber" && !bufferedSubscriber ? ["minimal_buffer_ms", "target_latency_ms"] : []),
      ...(role.startsWith("traffic-") ? ["id"] : []),
      ...(relationship ? [relationship] : []),
    ],
    common: commonFields,
    alwaysSet: role === "subscriber"
      ? ["media_client", ...(nativeSubscriber ? ["native_playback"] : [])]
      : [],
    inherited: inheritedNodeValues(role, config),
  });
  const actions = document.createElement("div");
  actions.className = "inspector-actions";
  if (!role.startsWith("traffic-")) {
    const duplicate = document.createElement("button");
    duplicate.textContent = "Duplicate";
    duplicate.addEventListener("click", () => duplicateNodes(role, id, 1));
    const bulk = document.createElement("button");
    bulk.textContent = "Bulk add";
    bulk.addEventListener("click", () => {
      const count = Number.parseInt(prompt("Number of copies", "10"), 10);
      if (Number.isInteger(count) && count > 0 && (count <= 500 || confirm(`Create ${count} nodes?`))) duplicateNodes(role, id, count);
    });
    actions.append(duplicate, bulk);
  }
  const remove = document.createElement("button");
  remove.className = "danger";
  remove.textContent = "Delete";
  remove.addEventListener("click", () => deleteNode(role, id));
  actions.append(remove);
  editor.append(actions);
}

function renderGlobalInspector() {
  const property = selected.property;
  heading(fieldTitle(property, resolveSchema(schema.properties[property])));
  const value = draft[property] || {};
  const fields = Object.keys(resolveSchema(schema.properties[property]).properties || {});
  renderObjectEditor(editor, schema.properties[property], value, (next) => commit(() => { draft[property] = next; }), {
    common: fields,
    inherited: schemaDefault(schema.properties[property]),
  });
}

function nodeOptions(select, selectedValue) {
  select.append(new Option("Select node", ""));
  for (const node of allNodes()) select.append(new Option(`${node.id} (${node.role})`, node.id));
  select.value = selectedValue;
}

function renderLinkInspector() {
  const index = selected.index;
  const link = draft.links?.[index];
  if (!link) { selected = null; return renderInspector(); }
  heading("Physical link");
  for (const side of ["from", "to"]) {
    const wrapper = document.createElement("div");
    wrapper.className = "field";
    const label = document.createElement("label");
    label.textContent = side;
    const select = document.createElement("select");
    nodeOptions(select, link[side]);
    select.addEventListener("change", () => {
      const other = side === "from" ? link.to : link.from;
      if (!select.value || select.value === other) return renderInspector();
      const duplicate = draft.links.some((item, otherIndex) => otherIndex !== index && canonicalPair(item.from, item.to) === canonicalPair(select.value, other));
      if (duplicate) { alert("Physical link already exists."); return renderInspector(); }
      commit(() => { draft.links[index][side] = select.value; });
    });
    wrapper.append(label, select);
    editor.append(wrapper);
  }
  for (const direction of ["forward", "reverse"]) {
    const fieldset = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = direction === "forward" ? `${link.from} to ${link.to}` : `${link.to} to ${link.from}`;
    fieldset.append(legend);
    renderObjectEditor(fieldset, definitions.DirectionSpec, link[direction] || {}, (next) => commit(() => {
      if (Object.keys(next).length) draft.links[index][direction] = next;
      else delete draft.links[index][direction];
    }), { common: Object.keys(definitions.DirectionSpec.properties) });
    editor.append(fieldset);
  }
  const actions = document.createElement("div");
  actions.className = "inspector-actions";
  const swap = document.createElement("button");
  swap.textContent = "Swap direction";
  swap.addEventListener("click", () => commit(() => {
    [link.from, link.to] = [link.to, link.from];
    [link.forward, link.reverse] = [link.reverse, link.forward];
  }));
  const remove = document.createElement("button");
  remove.className = "danger";
  remove.textContent = "Delete link";
  remove.addEventListener("click", () => deleteLink(index));
  actions.append(swap, remove);
  editor.append(actions);
}

function renderRouteInspector() {
  const name = selected.name;
  const route = draft.traffic?.routes?.[name];
  if (!route) { selected = null; return renderInspector(); }
  heading("Traffic route");
  idField("Route name", name, (next) => renameRoute(name, next));
  editor.append(renderField("path", definitions.TrafficRouteConfig.properties.path, route.path, true, (next) => commit(() => { route.path = next; })));
  const actions = document.createElement("div");
  actions.className = "inspector-actions";
  const shortest = document.createElement("button");
  shortest.textContent = "Shortest router path";
  shortest.addEventListener("click", () => {
    const path = shortestTrafficPath();
    if (!path) return alert("No sender-to-receiver path through routers exists.");
    commit(() => { route.path = path; });
  });
  const build = document.createElement("button");
  build.textContent = "Build on canvas";
  build.addEventListener("click", () => {
    const sender = draft.traffic?.sender?.id;
    if (!sender) return alert("Traffic sender missing.");
    routeBuild = name;
    routeBuildFlow = null;
    linkMode = false;
    linkStart = null;
    commit(() => { route.path = [sender]; selected = { kind: "route", name }; });
    linkPrompt.textContent = `Route starts at ${sender}. Select connected router(s), then receiver.`;
  });
  const remove = document.createElement("button");
  remove.className = "danger";
  remove.textContent = "Delete route";
  remove.addEventListener("click", () => deleteRoute(name));
  actions.append(build, shortest, remove);
  editor.append(actions);
}

function flowDefaults(kind, id, route) {
  if (kind === "cbr") return { id, kind, route, duration_s: 30, rate_mbps: 10 };
  if (kind === "segmented") return { id, kind, route, duration_s: 30, representation_sequence_mbps: [2, 5, 8] };
  return { id, kind: "bulk", route, duration_s: 30 };
}

function renderFlowInspector() {
  const index = selected.index;
  const flow = draft.traffic?.flows?.[index];
  if (!flow) { selected = null; return renderInspector(); }
  heading("Traffic load");
  idField("Load id", flow.id, (next) => {
    next = next.trim();
    if (!next || draft.traffic.flows.some((item, i) => i !== index && item.id === next)) return alert("Flow id must be unique.");
    commit(() => { flow.id = next; });
  });
  const kindWrap = document.createElement("div");
  kindWrap.className = "field";
  const kindLabel = document.createElement("label");
  kindLabel.textContent = "Kind";
  const kind = document.createElement("select");
  for (const value of Object.keys(manifest.flowKinds)) kind.append(new Option(value, value));
  kind.value = flow.kind;
  kind.addEventListener("change", () => commit(() => {
    draft.traffic.flows[index] = flowDefaults(kind.value, flow.id, flow.route);
  }));
  kindWrap.append(kindLabel, kind);
  editor.append(kindWrap);
  const route = draft.traffic.routes?.[flow.route];
  if (route) {
    const path = document.createElement("fieldset");
    const legend = document.createElement("legend");
    legend.textContent = "Path";
    path.append(legend, renderField("path", definitions.TrafficRouteConfig.properties.path, route.path, true, (next) => commit(() => { route.path = next; })));
    const actions = document.createElement("div");
    actions.className = "inspector-actions";
    const build = document.createElement("button");
    build.textContent = "Build on canvas";
    build.addEventListener("click", () => {
      const sender = draft.traffic?.sender?.id;
      if (!sender) return alert("Traffic sender missing.");
      routeBuild = flow.route;
      routeBuildFlow = index;
      linkMode = false;
      linkStart = null;
      commit(() => { route.path = [sender]; selected = { kind: "flow", index }; });
      linkPrompt.textContent = `Path starts at ${sender}. Select connected router(s), then receiver.`;
    });
    const shortest = document.createElement("button");
    shortest.textContent = "Shortest path";
    shortest.addEventListener("click", () => {
      const next = shortestTrafficPath();
      if (!next) return alert("No sender-to-receiver path through routers exists.");
      commit(() => { route.path = next; });
    });
    actions.append(build, shortest);
    path.append(actions);
    editor.append(path);
  }
  const routeDetails = document.createElement("details");
  const routeSummary = document.createElement("summary");
  routeSummary.textContent = "Advanced route sharing";
  const routeWrap = document.createElement("div");
  routeWrap.className = "field";
  const routeLabel = document.createElement("label");
  routeLabel.textContent = "Named route";
  const routeSelect = document.createElement("select");
  for (const name of Object.keys(draft.traffic.routes || {})) routeSelect.append(new Option(name, name));
  routeSelect.value = flow.route;
  routeSelect.addEventListener("change", () => commit(() => { flow.route = routeSelect.value; }));
  routeWrap.append(routeLabel, routeSelect);
  routeDetails.append(routeSummary, routeWrap);
  editor.append(routeDetails);
  const flowSchema = definitions[manifest.flowKinds[flow.kind]];
  renderObjectEditor(editor, flowSchema, flow, (next) => commit(() => { draft.traffic.flows[index] = next; }), {
    skip: ["id", "kind", "route"], common: ["start_s", "duration_s"],
  });
  const remove = document.createElement("button");
  remove.className = "danger";
  remove.textContent = "Delete load";
  remove.addEventListener("click", () => {
    if (!confirm(`Delete traffic load ${flow.id}?`)) return;
    commit(() => {
      const routeName = flow.route;
      draft.traffic.flows.splice(index, 1);
      const routeUnused = !draft.traffic.flows.some((item) => item.route === routeName);
      if (routeUnused && /^load-\d+-route$/.test(routeName)) delete draft.traffic.routes[routeName];
      selected = null;
    });
  });
  editor.append(remove);
}

function renderTrafficItems() {
  trafficItems.replaceChildren();
  trafficItems.className = "item-list";
  (draft.traffic?.flows || []).forEach((flow, index) => {
    const button = document.createElement("button");
    const path = draft.traffic.routes?.[flow.route]?.path || [];
    button.textContent = `${flow.id}: ${flow.kind} · ${path.join(" - ") || "no path"}`;
    button.addEventListener("click", () => { selected = { kind: "flow", index }; renderAll(); });
    trafficItems.append(button);
  });
}

function renderRouteItems() {
  routeItems.replaceChildren();
  routeItems.className = "item-list";
  for (const name of Object.keys(draft.traffic?.routes || {})) {
    const button = document.createElement("button");
    button.textContent = `Route: ${name}`;
    button.addEventListener("click", () => { selected = { kind: "route", name }; renderAll(); });
    routeItems.append(button);
  }
}

function renderSummary() {
  const parts = [
    `${Object.keys(draft.relays || {}).length} relays`,
    `${Object.keys(draft.routers || {}).length} routers`,
    `${Object.keys(draft.publishers || {}).length} publishers`,
    `${Object.keys(draft.subscribers || {}).length} subscribers`,
    `${(draft.links || []).length} links`,
  ];
  summaryEl.textContent = parts.join(", ");
  const shaped = (draft.links || []).some((link) => [link.forward, link.reverse].some((value) => value && Object.values(value).some((field) => field !== null && field !== undefined)));
  const containernet = Object.keys(draft.routers || {}).length || draft.traffic || shaped;
  backendBadge.textContent = containernet ? "Containernet required" : "Docker compatible";
  backendBadge.classList.toggle("containernet", Boolean(containernet));
}

function updateHistoryButtons() {
  undoButton.disabled = !history.length;
  redoButton.disabled = !future.length;
}

function renderAll() {
  renderGraph();
  renderInspector();
  renderTrafficItems();
  renderRouteItems();
  renderSummary();
  updateHistoryButtons();
}

function scheduleValidation() {
  valid = false;
  previewButton.disabled = true;
  downloadButton.disabled = true;
  validationEl.className = "validation pending";
  validationEl.textContent = "Validating…";
  clearTimeout(validationTimer);
  validationTimer = setTimeout(validate, 250);
}

async function validate() {
  const sequence = ++validationSequence;
  try {
    const response = await fetch("/api/designer/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(draft),
    });
    const payload = await response.json();
    if (sequence !== validationSequence) return;
    const localErrors = portCollisionErrors();
    valid = response.ok && payload.valid && !localErrors.length;
    renderValidation([...(payload.errors || []), ...localErrors], payload.error);
  } catch (error) {
    if (sequence !== validationSequence) return;
    renderValidation([], error.message);
  }
}

function renderValidation(errors, requestError) {
  validationEl.className = `validation ${valid ? "valid" : "invalid"}`;
  validationEl.textContent = valid ? "Topology valid" : requestError || `${errors.length} validation error(s)`;
  errorsEl.replaceChildren();
  for (const error of errors.slice(0, 30)) {
    const item = document.createElement("div");
    item.className = "error";
    item.textContent = `${error.loc?.join(".") || "topology"}: ${error.message}`;
    errorsEl.append(item);
  }
  previewButton.disabled = !valid;
  downloadButton.disabled = !valid;
}

async function yamlResponse() {
  const response = await fetch("/api/designer/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(draft),
  });
  if (!response.ok) {
    const payload = await response.json();
    renderValidation(payload.errors || [], payload.error);
    throw new Error("Topology is not valid.");
  }
  return response;
}

async function previewYaml() {
  const response = await yamlResponse();
  yamlPreview.value = await response.text();
  yamlDialog.showModal();
}

async function downloadYaml() {
  const response = await yamlResponse();
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = "topology.yaml";
  link.click();
  URL.revokeObjectURL(url);
}

async function importYaml(text) {
  const response = await fetch("/api/designer/import", {
    method: "POST",
    headers: { "Content-Type": "application/yaml" },
    body: text,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || "Import failed");
  history.push(snapshot());
  future = [];
  draft = payload.config;
  cleanSubscriberModeFields();
  positions = {};
  selected = null;
  autoLayout(false);
  renderAll();
  scheduleValidation();
  saveDraft();
}

async function loadExample() {
  const name = document.querySelector("#examples").value;
  if (!name) {
    if (!confirm("Replace current draft with blank topology?")) return;
    history.push(snapshot());
    draft = blankDraft();
    draft.defaults = schemaDefault(schema.properties.defaults);
    draft.startup = schemaDefault(schema.properties.startup);
  } else {
    if (!confirm(`Replace current draft with ${name}?`)) return;
    const response = await fetch(`/api/designer/examples/${encodeURIComponent(name)}`);
    const payload = await response.json();
    if (!response.ok) return alert(payload.error || "Could not load example.");
    history.push(snapshot());
    draft = payload.config;
    cleanSubscriberModeFields();
  }
  future = [];
  positions = {};
  selected = null;
  autoLayout(false);
  renderAll();
  scheduleValidation();
  saveDraft();
}

async function initialize() {
  const [schemaResponse, examplesResponse] = await Promise.all([
    fetch("/api/designer/schema"),
    fetch("/api/designer/examples"),
  ]);
  const payload = await schemaResponse.json();
  const examplesPayload = await examplesResponse.json();
  schema = payload.schema;
  definitions = schema.$defs;
  manifest = payload.manifest;
  digest = payload.digest;
  draft = payload.initial_config || blankDraft();
  const savedText = localStorage.getItem(`moqlab-designer:${digest}`);
  let restored = false;
  if (savedText && confirm("Restore autosaved topology draft and layout?")) {
    try {
      const saved = JSON.parse(savedText);
      draft = saved.draft;
      positions = saved.positions || {};
      restored = true;
    } catch (_error) {
      localStorage.removeItem(`moqlab-designer:${digest}`);
    }
  }
  if (!payload.initial_config && !restored) {
    draft.defaults = schemaDefault(schema.properties.defaults);
    draft.startup = schemaDefault(schema.properties.startup);
  }
  cleanSubscriberModeFields();
  if (!Object.keys(positions).length) autoLayout(false);
  const select = document.querySelector("#examples");
  for (const name of examplesPayload.examples) select.append(new Option(name, name));
  const globalButtons = document.querySelector("#global-config-buttons");
  for (const property of manifest.genericSections) {
    const button = document.createElement("button");
    button.className = "wide";
    button.type = "button";
    button.textContent = fieldTitle(property, schema.properties[property]);
    button.addEventListener("click", () => { selected = { kind: "global", property }; renderAll(); });
    globalButtons.append(button);
  }
  renderAll();
  scheduleValidation();
}

for (const button of document.querySelectorAll("#node-palette button")) {
  button.addEventListener("click", () => addNode(button.dataset.role));
  button.addEventListener("dragstart", (event) => event.dataTransfer.setData("text/moqlab-role", button.dataset.role));
}
document.querySelector("#link-mode").addEventListener("click", () => {
  routeBuild = null;
  routeBuildFlow = null;
  linkMode = true;
  linkStart = null;
  linkPrompt.textContent = "Select first node, then second node.";
});
document.querySelector("#add-load").addEventListener("click", addTrafficLoad);
document.querySelector("#add-route").addEventListener("click", addRoute);
document.querySelector("#add-flow").addEventListener("click", addFlow);
document.querySelector("#load-example").addEventListener("click", loadExample);
document.querySelector("#import-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  try { await importYaml(await file.text()); } catch (error) { alert(error.message); }
  event.target.value = "";
});
undoButton.addEventListener("click", undo);
redoButton.addEventListener("click", redo);
document.querySelector("#auto-layout").addEventListener("click", () => autoLayout(true));
previewButton.addEventListener("click", () => previewYaml().catch((error) => alert(error.message)));
downloadButton.addEventListener("click", () => downloadYaml().catch((error) => alert(error.message)));
document.addEventListener("keydown", (event) => {
  if (!(event.ctrlKey || event.metaKey)) return;
  if (event.key.toLowerCase() === "z") {
    event.preventDefault();
    event.shiftKey ? redo() : undo();
  } else if (event.key.toLowerCase() === "y") {
    event.preventDefault();
    redo();
  }
});

initialize().catch((error) => {
  validationEl.className = "validation invalid";
  validationEl.textContent = `Designer failed to load: ${error.message}`;
});
