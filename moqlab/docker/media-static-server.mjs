import { createReadStream, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";

const root = "/opt/warp-player";
const mime = { ".html": "text/html", ".js": "text/javascript", ".json": "application/json", ".png": "image/png", ".map": "application/json" };

createServer((request, response) => {
  const raw = new URL(request.url ?? "/", "http://localhost").pathname;
  const relative = normalize(raw).replace(/^(\.\.(\/|\\|$))+/, "");
  let path = join(root, relative === "/" ? "index.html" : relative);
  try {
    if (statSync(path).isDirectory()) path = join(path, "index.html");
    response.writeHead(200, { "content-type": mime[extname(path)] ?? "application/octet-stream" });
    createReadStream(path).pipe(response);
  } catch {
    response.writeHead(404).end("not found");
  }
}).listen(8080, "127.0.0.1");
