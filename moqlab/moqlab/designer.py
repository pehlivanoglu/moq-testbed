from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml
from pydantic import ValidationError

from moqlab.config.schema import TopologyConfig
from moqlab.exceptions import ConfigError

_log = logging.getLogger(__name__)
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_STATIC_ROOT = _PROJECT_ROOT / "visualizer"
_EXAMPLES_ROOT = _PROJECT_ROOT / "configs" / "examples"
_MAX_REQUEST_BYTES = 1024 * 1024
_STATIC_FILES = {
    "/": ("designer.html", "text/html; charset=utf-8"),
    "/designer.js": ("designer.js", "application/javascript; charset=utf-8"),
    "/designer.css": ("designer.css", "text/css; charset=utf-8"),
}

UI_MANIFEST: dict[str, object] = {
    "topLevelOrder": [
        "topology_mode",
        "defaults",
        "startup",
        "relays",
        "publishers",
        "subscribers",
        "routers",
        "traffic",
        "links",
    ],
    "genericSections": ["defaults", "startup"],
    "nodeCollections": {
        "relay": {"property": "relays", "definition": "RelayConfig"},
        "publisher": {"property": "publishers", "definition": "PublisherConfig"},
        "subscriber": {"property": "subscribers", "definition": "SubscriberConfig"},
        "router": {"property": "routers", "definition": "RouterConfig"},
    },
    "trafficEndpoints": ["sender", "receiver"],
    "flowKinds": {
        "bulk": "BulkTrafficFlow",
        "cbr": "CbrTrafficFlow",
        "segmented": "SegmentedTrafficFlow",
    },
    "relationships": {
        "relay": "upstream",
        "publisher": "connects_to",
        "subscriber": "connects_to",
    },
    "manualSchemaPaths": [
        "TopologyConfig.properties.relays",
        "TopologyConfig.properties.publishers",
        "TopologyConfig.properties.subscribers",
        "TopologyConfig.properties.routers",
        "TopologyConfig.properties.links",
        "TopologyConfig.$defs.TrafficConfig.properties.routes",
        "TopologyConfig.$defs.TrafficConfig.properties.flows",
    ],
}

_ALLOWED_SCHEMA_KEYWORDS = {
    "$defs",
    "$ref",
    "additionalProperties",
    "anyOf",
    "const",
    "default",
    "description",
    "discriminator",
    "enum",
    "exclusiveMaximum",
    "exclusiveMinimum",
    "format",
    "items",
    "maxItems",
    "maxLength",
    "maxProperties",
    "maximum",
    "minItems",
    "minLength",
    "minProperties",
    "minimum",
    "multipleOf",
    "oneOf",
    "pattern",
    "properties",
    "required",
    "title",
    "type",
}


def topology_json_schema() -> dict[str, object]:
    return TopologyConfig.model_json_schema(by_alias=True)


def schema_digest(schema: dict[str, object] | None = None) -> str:
    value = schema or topology_json_schema()
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def schema_contract_issues() -> list[str]:
    schema = topology_json_schema()
    issues: list[str] = []
    properties = schema.get("properties", {})
    if list(properties) != UI_MANIFEST["topLevelOrder"]:
        issues.append("top-level schema properties differ from UI manifest")

    definitions = schema.get("$defs", {})
    node_collections = UI_MANIFEST["nodeCollections"]
    assert isinstance(node_collections, dict)
    for role, spec in node_collections.items():
        assert isinstance(spec, dict)
        prop = properties.get(spec["property"], {})
        ref = prop.get("additionalProperties", {}).get("$ref")
        expected = f"#/$defs/{spec['definition']}"
        if ref != expected:
            issues.append(f"{role} collection does not resolve to {expected}")

    traffic = definitions.get("TrafficConfig", {})
    flow_items = traffic.get("properties", {}).get("flows", {}).get("items", {})
    mapping = flow_items.get("discriminator", {}).get("mapping", {})
    expected_kinds = UI_MANIFEST["flowKinds"]
    assert isinstance(expected_kinds, dict)
    if set(mapping) != set(expected_kinds):
        issues.append("traffic flow discriminator differs from UI manifest")

    def visit(node: object, path: str) -> None:
        if not isinstance(node, dict):
            return
        unknown = set(node) - _ALLOWED_SCHEMA_KEYWORDS
        if unknown:
            issues.append(f"unsupported JSON Schema keyword(s) at {path}: {sorted(unknown)}")
        manual_paths = UI_MANIFEST["manualSchemaPaths"]
        assert isinstance(manual_paths, list)
        if (
            isinstance(node.get("additionalProperties"), dict)
            and not node.get("properties")
            and path not in manual_paths
        ):
            issues.append(f"schema-driven map at {path} needs editor support")
        item_schema = node.get("items")
        if (
            isinstance(item_schema, dict)
            and resolve_schema_type(item_schema, schema) == "object"
            and path not in manual_paths
        ):
            issues.append(f"object array at {path} needs editor support")
        for name, child in node.get("$defs", {}).items():
            visit(child, f"{path}.$defs.{name}")
        for name, child in node.get("properties", {}).items():
            visit(child, f"{path}.properties.{name}")
        additional = node.get("additionalProperties")
        if isinstance(additional, dict):
            visit(additional, f"{path}.additionalProperties")
        items = node.get("items")
        if isinstance(items, dict):
            visit(items, f"{path}.items")
        for keyword in ("anyOf", "oneOf"):
            for index, child in enumerate(node.get(keyword, [])):
                visit(child, f"{path}.{keyword}[{index}]")

    visit(schema, "TopologyConfig")
    return issues


def resolve_schema_type(node: dict[str, object], root: dict[str, object]) -> str | None:
    current = node
    seen: set[str] = set()
    while isinstance(current.get("$ref"), str):
        ref = str(current["$ref"])
        if ref in seen or not ref.startswith("#/$defs/"):
            return None
        seen.add(ref)
        current = root.get("$defs", {}).get(ref.rsplit("/", 1)[-1], {})  # type: ignore[assignment,union-attr]
    if current.get("type") == "object" or "properties" in current:
        return "object"
    for branch in [*current.get("oneOf", []), *current.get("anyOf", [])]:  # type: ignore[misc]
        if isinstance(branch, dict) and resolve_schema_type(branch, root) == "object":
            return "object"
    return current.get("type") if isinstance(current.get("type"), str) else None


def example_names() -> list[str]:
    return sorted(path.name for path in _EXAMPLES_ROOT.glob("*.yaml") if path.is_file())


def parse_yaml_draft(text: str, *, source: str = "topology") -> dict[str, object]:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise ConfigError(f"failed to parse {source}: {error}") from error
    if raw is None:
        raise ConfigError(f"{source} is empty")
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: top-level value must be a mapping")
    try:
        validate_draft(raw)
    except ValidationError as error:
        raise DraftValidationError(source, error) from error
    return normalized_draft(raw)


def load_example_draft(name: str) -> dict[str, object]:
    if name not in example_names():
        raise FileNotFoundError(name)
    return parse_yaml_draft((_EXAMPLES_ROOT / name).read_text(), source=name)


def validation_errors(error: ValidationError) -> list[dict[str, object]]:
    return [
        {
            "loc": [str(part) for part in item["loc"]],
            "message": item["msg"],
            "type": item["type"],
        }
        for item in error.errors(include_url=False, include_context=False)
    ]


def validate_draft(raw: object) -> TopologyConfig:
    return TopologyConfig.model_validate(raw)


def normalized_draft(raw: dict[str, object]) -> dict[str, object]:
    result: dict[str, object] = {"topology_mode": "explicit"}
    result.update(deepcopy(raw))
    result["topology_mode"] = "explicit"
    return result


def dump_draft_yaml(raw: dict[str, object]) -> str:
    validate_draft(raw)
    return yaml.safe_dump(
        normalized_draft(raw),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )


class DesignerHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        initial_config: dict[str, object] | None = None,
    ) -> None:
        super().__init__(server_address, _DesignerHandler)
        self.initial_config = deepcopy(initial_config)


def make_designer_server(
    *, host: str = "127.0.0.1", port: int = 8765, config_path: Path | None = None
) -> DesignerHTTPServer:
    initial = None
    if config_path is not None:
        initial = parse_yaml_draft(config_path.read_text(), source=str(config_path))
    return DesignerHTTPServer((host, port), initial_config=initial)


class _DesignerHandler(BaseHTTPRequestHandler):
    server: DesignerHTTPServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/designer/schema":
            schema = topology_json_schema()
            self._send_json(
                HTTPStatus.OK,
                {
                    "schema": schema,
                    "manifest": UI_MANIFEST,
                    "digest": schema_digest(schema),
                    "initial_config": self.server.initial_config,
                },
            )
            return
        if path == "/api/designer/examples":
            self._send_json(HTTPStatus.OK, {"examples": example_names()})
            return
        prefix = "/api/designer/examples/"
        if path.startswith(prefix):
            name = unquote(path[len(prefix):])
            try:
                config = load_example_draft(name)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            except ConfigError as error:
                self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, {"name": name, "config": config})
            return
        static_file = _STATIC_FILES.get(path)
        if static_file is not None:
            self._send_static(*static_file)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self._read_body()
        except _RequestError as error:
            self._send_json(error.status, {"error": error.message})
            return

        if path == "/api/designer/import":
            try:
                config = parse_yaml_draft(body.decode("utf-8"), source="uploaded YAML")
            except DraftValidationError as error:
                self._send_json(
                    HTTPStatus.UNPROCESSABLE_ENTITY,
                    {"valid": False, "errors": error.errors},
                )
                return
            except (UnicodeDecodeError, ConfigError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
                return
            self._send_json(HTTPStatus.OK, {"valid": True, "config": config})
            return

        if path not in {"/api/designer/validate", "/api/designer/export"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            raw = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid JSON: {error}"})
            return
        if not isinstance(raw, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "config must be a JSON object"})
            return
        try:
            validate_draft(raw)
        except ValidationError as error:
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"valid": False, "errors": validation_errors(error)},
            )
            return

        if path == "/api/designer/validate":
            self._send_json(
                HTTPStatus.OK,
                {"valid": True, "errors": [], "config": normalized_draft(raw)},
            )
            return
        yaml_text = dump_draft_yaml(raw)
        self._send_body(
            HTTPStatus.OK,
            "application/yaml; charset=utf-8",
            yaml_text.encode(),
            {"Content-Disposition": 'attachment; filename="topology.yaml"'},
        )

    def _read_body(self) -> bytes:
        value = self.headers.get("Content-Length")
        try:
            length = int(value) if value is not None else -1
        except ValueError as error:
            raise _RequestError(HTTPStatus.BAD_REQUEST, "invalid Content-Length") from error
        if length < 0:
            raise _RequestError(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
        if length > _MAX_REQUEST_BYTES:
            raise _RequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request exceeds 1 MiB")
        return self.rfile.read(length)

    def _send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self._send_body(status, "application/json", body)

    def _send_static(self, filename: str, content_type: str) -> None:
        try:
            body = (_STATIC_ROOT / filename).read_bytes()
        except FileNotFoundError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_body(HTTPStatus.OK, content_type, body)

    def _send_body(
        self,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'")
            for name, value in (extra_headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            _log.debug("designer client disconnected before response completed")

    def log_message(self, fmt: str, *args: object) -> None:
        _log.debug("designer: " + fmt, *args)


class _RequestError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class DraftValidationError(ConfigError):
    def __init__(self, source: str, error: ValidationError) -> None:
        super().__init__(f"{source}: {error}")
        self.errors = validation_errors(error)
