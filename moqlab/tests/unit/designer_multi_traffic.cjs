const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

class Element {
  constructor() { this.children = []; this.listeners = {}; }
  append(...children) { this.children.push(...children); }
  addEventListener(event, callback) { this.listeners[event] = callback; }
}
const alerts = [];
const context = vm.createContext({
  structuredClone,
  document: {
    querySelector: () => new Element(),
    querySelectorAll: () => [],
    createElement: () => new Element(),
    addEventListener() {},
  },
  Option: function(text, value) { this.text = text; this.value = value; },
  alert: (message) => alerts.push(message),
  confirm: () => true,
  assert,
  input: JSON.parse(fs.readFileSync(0, "utf8")),
  same: (actual, expected) => assert.equal(JSON.stringify(actual), JSON.stringify(expected)),
});
const source = fs.readFileSync(process.argv[2], "utf8");
vm.runInContext(source.slice(0, source.lastIndexOf("\ninitialize().catch")), context);
vm.runInContext(`
  schema = input.schema;
  definitions = schema.$defs;
  manifest = input.manifest;
  renderAll = () => {};
  scheduleValidation = () => {};
  saveDraft = () => {};
  addNode("traffic-sender");
  addNode("traffic-sender");
  addNode("traffic-receiver");
  addNode("traffic-receiver");
  same(draft.traffic.senders.map(e => e.id), ["traffic-tx", "traffic-tx-1"]);
  same(draft.traffic.receivers.map(e => e.id), ["traffic-rx", "traffic-rx-1"]);
  renameNode("traffic-sender", "traffic-tx-1", "tx2");
  assert.equal(nodeConfig("traffic-sender", "tx2").id, "tx2");
  duplicateNodes("traffic-receiver", "traffic-rx-1", 1);
  assert.equal(draft.traffic.receivers.length, 3);
  deleteNode("traffic-receiver", selected.id);
  assert.equal(draft.traffic.receivers.length, 2);
  undo();
  assert.equal(draft.traffic.receivers.length, 3);
  redo();
  assert.equal(draft.traffic.receivers.length, 2);
  draft.routers = {router: {}};
  draft.switches = {switch: {}};
  draft.links = [
    {from: "traffic-tx", to: "router"}, {from: "tx2", to: "router"},
    {from: "router", to: "traffic-rx"}, {from: "router", to: "switch"},
    {from: "switch", to: "traffic-rx-1"},
  ];
  selected = {kind: "node", role: "traffic-sender", id: "tx2"};
  addTrafficLoad();
  let route = draft.traffic.routes[draft.traffic.flows[0].route];
  same(route.path, ["tx2", "router", "traffic-rx"]);
  const wrapper = document.createElement("div");
  renderTrafficEndpoints(wrapper, route);
  const receiverSelect = wrapper.children[1].children[1];
  receiverSelect.value = "traffic-rx-1";
  receiverSelect.listeners.change();
  same(route.path, ["tx2", "router", "switch", "traffic-rx-1"]);
  selected = {kind: "node", role: "traffic-receiver", id: "traffic-rx-1"};
  renameNode("traffic-receiver", "traffic-rx-1", "rx2");
  assert.equal(route.path.at(-1), "rx2");
  const routeName = draft.traffic.flows[0].route;
  routeBuild = routeName;
  appendRouteNode("traffic-tx");
  appendRouteNode("router");
  appendRouteNode("switch");
  appendRouteNode("rx2");
  same(route.path, ["traffic-tx", "router", "switch", "rx2"]);
  assert.equal(routeBuild, null);
  deleteNode("traffic-receiver", "rx2");
  assert.equal(draft.traffic.receivers.length, 2);
`, context);
assert.equal(alerts.length, 1);
assert.match(alerts[0], /referenced by/);
