import { writeFileSync } from "node:fs";

const options = Object.fromEntries(process.argv.slice(2).map((arg) => {
  const [key, ...value] = arg.replace(/^--/, "").split("=");
  return [key, value.join("=")];
}));
const readyPath = "/tmp/moqlab-media-ready.json";
const failurePath = "/tmp/moqlab-media-failure.json";
const timeoutMs = Number(options["ready-timeout-s"] ?? 30) * 1000;
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const emit = (event, detail = {}) => console.log(JSON.stringify({ event, at: Date.now(), ...detail }));

async function endpoint(path, init) {
  const response = await fetch(`http://127.0.0.1:9222${path}`, init);
  if (!response.ok) throw new Error(`CDP HTTP ${response.status}: ${path}`);
  return response.json();
}

async function connectCdp() {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const pages = await endpoint("/json/list");
      const page = pages.find(({ type }) => type === "page");
      if (!page?.webSocketDebuggerUrl) throw new Error("Chromium page target not ready");
      const socket = new WebSocket(page.webSocketDebuggerUrl);
      await new Promise((resolve, reject) => {
        socket.onopen = resolve;
        socket.onerror = () => reject(new Error("CDP WebSocket failed"));
      });
      return socket;
    } catch { await delay(200); }
  }
  throw new Error("Chromium debugging endpoint did not become ready");
}

const socket = await connectCdp();
let nextId = 1;
const pending = new Map();
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(message.error.message)) : resolve(message.result);
  } else if (message.method === "Runtime.consoleAPICalled") {
    const consoleText = message.params.args.map((arg) => arg.value ?? arg.description ?? "").join(" ");
    emit("browser_console", {
      level: message.params.type,
      text: consoleText,
    });
    const subscription = /Successfully subscribed to (.+):(video\/\S+) with trackAlias (\d+)/.exec(consoleText);
    if (subscription) {
      emit("subscription", {
        namespace: subscription[1],
        track: subscription[2],
        alias: Number(subscription[3]),
      });
    }
  } else if (message.method === "Runtime.exceptionThrown") {
    emit("browser_error", {
      text: message.params.exceptionDetails?.exception?.description ?? message.params.exceptionDetails?.text,
    });
  }
};
function call(method, params = {}) {
  const id = nextId++;
  const response = new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  socket.send(JSON.stringify({ id, method, params }));
  return response;
}
async function evaluate(expression) {
  const result = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
  return result.result.value;
}

try {
  await call("Page.enable");
  await call("Runtime.enable");
  const query = new URLSearchParams({
    serverUrl: options["server-url"],
    fingerprintUrl: options["fingerprint-url"],
    namespace: options.namespace,
  });
  await call("Page.navigate", { url: `http://127.0.0.1:8080/?${query}` });
  await delay(500);
  await evaluate(`(async () => {
    const wait = async (label, test, timeout = ${timeoutMs}) => {
      const end = Date.now() + timeout;
      while (Date.now() < end) { if (test()) return; await new Promise(r => setTimeout(r, 100)); }
      throw new Error(label + " wait timed out");
    };
    await wait("connect button", () => document.querySelector("#connectBtn:not(:disabled)"));
    document.getElementById("engineChoice").value = "webcodecs";
    document.getElementById("catalogMode").value = "subscribe";
    document.getElementById("minimalBuffer").value = ${JSON.stringify(options["minimal-buffer-ms"])};
    document.getElementById("targetLatency").value = ${JSON.stringify(options["target-latency-ms"])};
    document.getElementById("connectBtn").click();
    await wait("video catalog", () => document.getElementById("video-tracks-select"));
    const select = document.getElementById("video-tracks-select");
    const option = [...select.options].find(o => o.value === ${JSON.stringify(options["video-track"])});
    if (!option) throw new Error("video track not found: " + ${JSON.stringify(options["video-track"])});
    if (option.disabled) throw new Error("video track disabled: " + option.title);
    select.value = option.value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
    document.getElementById("minimalBuffer").dispatchEvent(new Event("change", { bubbles: true }));
    document.getElementById("targetLatency").dispatchEvent(new Event("change", { bubbles: true }));
    document.getElementById("startBtn").click();
  })()`);

  const sid = /\/s(\d+)$/.exec(options["video-track"] ?? "");
  emit("subscription_target", { track: options["video-track"], expectedSubscriptions: sid ? Number(sid[1]) + 1 : 1 });
  const expected = await evaluate(`(() => {
    const option = document.querySelector("#video-tracks-select option:checked");
    const match = /Resolution: (\\d+)×(\\d+)/.exec(option?.title ?? "");
    return match ? { width: Number(match[1]), height: Number(match[2]) } : null;
  })()`);
  if (!expected) throw new Error("selected track has no catalog resolution");
  const hashes = new Set();
  let evidence;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    evidence = await evaluate(`(() => {
      const canvas = document.getElementById("webcodecsCanvas");
      if (!canvas || !canvas.width || !canvas.height) return null;
      const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
      let hash = 2166136261, nonBlack = 0;
      const stride = Math.max(4, Math.floor(data.length / 4096 / 4) * 4);
      for (let i = 0; i < data.length; i += stride) {
        if (data[i] > 8 || data[i + 1] > 8 || data[i + 2] > 8) nonBlack++;
        hash = Math.imul(hash ^ data[i], 16777619);
        hash = Math.imul(hash ^ data[i + 1], 16777619);
        hash = Math.imul(hash ^ data[i + 2], 16777619);
      }
      return { width: canvas.width, height: canvas.height, nonBlack, hash: hash >>> 0 };
    })()`);
    if (evidence?.nonBlack > 0) hashes.add(evidence.hash);
    if (evidence?.width === expected.width && evidence?.height === expected.height && evidence.nonBlack > 0 && hashes.size >= 2) break;
    await delay(300);
  }
  if (!evidence?.nonBlack) throw new Error("decoded canvas stayed black");
  if (hashes.size < 2) throw new Error("decoded canvas stayed static");
  if (evidence.width !== expected.width || evidence.height !== expected.height) {
    throw new Error(`canvas resolution ${evidence.width}x${evidence.height} did not match ${expected.width}x${expected.height}`);
  }
  const shot = await call("Page.captureScreenshot", { format: "png" });
  writeFileSync("/tmp/moqlab-first-frame.png", Buffer.from(shot.data, "base64"));
  const ready = { status: "ready", track: options["video-track"], ...evidence, changingFrames: hashes.size };
  writeFileSync(readyPath, JSON.stringify(ready));
  emit("playback_ready", ready);
  emit("resolution", { width: evidence.width, height: evidence.height });

  let previousTrack = options["video-track"];
  for (;;) {
    await delay(1000);
    const selected = await evaluate(`document.getElementById("video-tracks-select")?.value ?? null`);
    if (selected && selected !== previousTrack) {
      emit("quality_switch", { from: previousTrack, to: selected });
      previousTrack = selected;
    }
  }
} catch (error) {
  const failure = { status: "failed", error: error instanceof Error ? error.message : String(error) };
  writeFileSync(failurePath, JSON.stringify(failure));
  emit("playback_error", failure);
  process.exitCode = 1;
  socket.close();
}
