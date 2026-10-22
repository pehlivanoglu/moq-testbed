from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

import yaml
from pydantic import ValidationError

from moqlab.designer import UI_MANIFEST, _DesignerHandler, _RequestError, topology_json_schema, validation_errors
from moqlab.exceptions import ConfigError
from simulation.runner import ROOT
from simulation.schema import Experiment, SimulationConfig, document, load_config, resolved_config


def editor_config(config: SimulationConfig) -> dict[str, object]:
    draft = document(config)["topology"]
    draft["experiment"] = config.experiment.model_dump(mode="json")
    return draft


def parse_editor(raw: dict[str, object]) -> SimulationConfig:
    topology = {key: value for key, value in raw.items() if key != "experiment"}
    config = SimulationConfig.model_validate({"topology": topology, "experiment": raw.get("experiment", {})})
    resolved_config(config)
    return config


def editor_schema() -> tuple[dict[str, object], dict[str, object]]:
    schema = topology_json_schema()
    sbd = schema["$defs"]["SbdConfig"]["properties"]
    sbd["algorithm"]["enum"] = ["PracticalPassiveAndRFC"]
    sbd["delay_source"]["enum"] = ["owd"]
    experiment = Experiment.model_json_schema()
    schema["$defs"].update(experiment.pop("$defs", {}))
    schema["properties"]["experiment"] = experiment
    manifest = deepcopy(UI_MANIFEST)
    manifest["simulation"] = True
    manifest["genericSections"] = ["experiment", "defaults"]
    manifest["topLevelOrder"].append("experiment")
    return schema, manifest


class SimulationHandler(_DesignerHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/designer/schema":
            schema, manifest = editor_schema()
            digest = "simulation-" + hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:16]
            self._send_json(HTTPStatus.OK, {"schema": schema, "manifest": manifest, "digest": digest,
                                          "initial_config": self.server.initial_config})
        elif path == "/api/designer/examples":
            self._send_json(HTTPStatus.OK, {"examples": sorted(p.name for p in (ROOT / "examples").glob("*.yaml"))})
        elif path.startswith("/api/designer/examples/"):
            name = unquote(path.rsplit("/", 1)[-1])
            if name not in {p.name for p in (ROOT / "examples").glob("*.yaml")}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self._send_json(HTTPStatus.OK, {"name": name, "config": editor_config(load_config(ROOT / "examples" / name))})
        else:
            super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/designer/import", "/api/designer/validate", "/api/designer/export"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = self._read_body()
            if path.endswith("/import"):
                raw = yaml.safe_load(body.decode())
                if isinstance(raw, dict) and "topology" in raw:
                    config = SimulationConfig.model_validate(raw)
                    resolved_config(config)
                else:
                    config = parse_editor(raw)
            else:
                raw = json.loads(body)
                config = parse_editor(raw)
            if path.endswith("/export"):
                text = yaml.safe_dump(document(config), sort_keys=False)
                self._send_body(HTTPStatus.OK, "application/yaml; charset=utf-8", text.encode(),
                                {"Content-Disposition": 'attachment; filename="simulation.yaml"'})
            else:
                self._send_json(HTTPStatus.OK, {"valid": True, "errors": [], "config": editor_config(config)})
        except _RequestError as error:
            self._send_json(error.status, {"valid": False, "error": error.message})
        except ValidationError as error:
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"valid": False, "errors": validation_errors(error)})
        except (ValueError, TypeError, AttributeError, UnicodeError, yaml.YAMLError, ConfigError) as error:
            self._send_json(HTTPStatus.BAD_REQUEST, {"valid": False, "error": str(error)})


def make_server(config: Path | None, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), SimulationHandler)
    server.initial_config = editor_config(load_config(config or ROOT / "examples" / "shared.yaml"))
    return server
