from __future__ import annotations

import json
import shutil
import subprocess
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import yaml
from click.testing import CliRunner

from moqlab import cli as cli_module
from moqlab.cli import cli
from moqlab.config.schema import TopologyConfig
from moqlab.designer import (
    DesignerHTTPServer,
    UI_MANIFEST,
    dump_draft_yaml,
    example_names,
    load_example_draft,
    parse_yaml_draft,
    schema_contract_issues,
    schema_digest,
    topology_json_schema,
)


def _valid_config() -> dict[str, object]:
    return {
        "topology_mode": "explicit",
        "relays": {"relay-a": {"listen_port": 9668, "admin_port": 9669}},
    }


@pytest.fixture
def designer_url():
    server = DesignerHTTPServer(("127.0.0.1", 0), initial_config=_valid_config())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(url: str, path: str, body: bytes | None = None, content_type: str = "application/json"):
    request = Request(
        url + path,
        data=body,
        headers={"Content-Type": content_type} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        response = urlopen(request, timeout=2)
    except HTTPError as error:
        return error.code, error.headers, error.read()
    with response:
        return response.status, response.headers, response.read()


def test_schema_manifest_covers_current_topology_schema() -> None:
    schema = topology_json_schema()

    assert schema_contract_issues() == []
    assert list(schema["properties"]) == UI_MANIFEST["topLevelOrder"]
    assert set(UI_MANIFEST["flowKinds"]) == {"bulk", "cbr", "segmented"}
    assert len(schema_digest(schema)) == 16


def test_all_examples_round_trip_semantically() -> None:
    examples_root = Path(__file__).resolve().parents[2] / "configs" / "examples"

    assert example_names()
    for name in example_names():
        raw = yaml.safe_load((examples_root / name).read_text())
        draft = load_example_draft(name)
        exported = yaml.safe_load(dump_draft_yaml(draft))

        assert exported["topology_mode"] == "explicit"
        assert TopologyConfig.model_validate(exported).model_dump(mode="json") == (
            TopologyConfig.model_validate(raw).model_dump(mode="json")
        )


def test_parse_yaml_rejects_non_mapping_and_invalid_topology() -> None:
    with pytest.raises(Exception, match="top-level value must be a mapping"):
        parse_yaml_draft("- nope\n")
    with pytest.raises(Exception, match="at least one relay"):
        parse_yaml_draft("topology_mode: explicit\n")


def test_designer_schema_examples_validate_and_export_apis(designer_url: str) -> None:
    status, headers, body = _request(designer_url, "/api/designer/schema")
    payload = json.loads(body)
    assert status == 200
    assert payload["initial_config"] == _valid_config()
    assert payload["manifest"] == UI_MANIFEST
    assert headers["Cache-Control"] == "no-store"

    status, _, body = _request(designer_url, "/api/designer/examples")
    assert status == 200
    assert "external_traffic.yaml" in json.loads(body)["examples"]

    status, _, body = _request(
        designer_url,
        "/api/designer/validate",
        json.dumps(_valid_config()).encode(),
    )
    assert status == 200
    assert json.loads(body)["valid"] is True

    status, headers, body = _request(
        designer_url,
        "/api/designer/export",
        json.dumps(_valid_config()).encode(),
    )
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="topology.yaml"'
    TopologyConfig.model_validate(yaml.safe_load(body))


def test_designer_api_reports_bad_and_invalid_input(designer_url: str) -> None:
    status, _, _ = _request(designer_url, "/api/designer/validate", b"not json")
    assert status == 400

    status, _, body = _request(designer_url, "/api/designer/validate", b"{}")
    assert status == 422
    assert json.loads(body)["errors"]

    status, _, _ = _request(
        designer_url,
        "/api/designer/import",
        b"relays: [",
        "application/yaml",
    )
    assert status == 400

    status, _, body = _request(
        designer_url,
        "/api/designer/import",
        b"topology_mode: explicit\n",
        "application/yaml",
    )
    assert status == 422
    assert json.loads(body)["valid"] is False

    status, _, _ = _request(designer_url, "/api/designer/examples/%2e%2e%2fREADME.md")
    assert status == 404


def test_designer_api_rejects_requests_over_one_mib(designer_url: str) -> None:
    status, _, body = _request(
        designer_url,
        "/api/designer/validate",
        b" " * (1024 * 1024 + 1),
    )
    assert status == 413
    assert "1 MiB" in json.loads(body)["error"]


def test_designer_assets_include_editor_controls_and_valid_javascript() -> None:
    root = Path(__file__).resolve().parents[2] / "visualizer"
    html = (root / "designer.html").read_text()
    app = root / "designer.js"

    assert 'id="node-palette"' in html
    assert 'id="add-load"' in html
    assert "Advanced routes" in html
    assert 'aria-label="Editable moqlab topology graph"' in html
    assert "/api/designer/validate" in app.read_text()
    assert "manifest.flowKinds" in app.read_text()
    assert "event.button !== 0 || linkMode || routeBuild" in app.read_text()
    assert 'selected = { kind: "node", role: node.role, id: node.id }' in app.read_text()
    assert "if (suppressGraphClick)" in app.read_text()
    assert "function inheritedNodeValues" in app.read_text()
    assert "function allocatePort" in app.read_text()
    assert "function portCollisionErrors" in app.read_text()
    assert '["publisher", "subscriber"].includes(role) ? ["kind"]' in app.read_text()
    assert "function addTrafficLoad" in app.read_text()
    assert 'heading("Traffic load")' in app.read_text()
    if node := shutil.which("node"):
        subprocess.run([node, "--check", str(app)], check=True)


def test_design_command_starts_local_server_and_closes_it(monkeypatch, tmp_path: Path) -> None:
    config = tmp_path / "topology.yaml"
    config.write_text(yaml.safe_dump(_valid_config()))

    class FakeServer:
        server_address = ("127.0.0.1", 9012)
        closed = False

        def serve_forever(self) -> None:
            raise KeyboardInterrupt

        def server_close(self) -> None:
            self.closed = True

    server = FakeServer()
    seen = {}

    def fake_make_server(*, host, port, config_path):
        seen.update(host=host, port=port, config_path=config_path)
        return server

    monkeypatch.setattr(cli_module, "make_designer_server", fake_make_server)

    result = CliRunner().invoke(cli, ["design", "-c", str(config), "--port", "9012"])

    assert result.exit_code == 0
    assert "designer: http://127.0.0.1:9012/" in result.output
    assert "stopped designer" in result.output
    assert seen == {"host": "127.0.0.1", "port": 9012, "config_path": config}
    assert server.closed


def test_design_command_reports_port_collision(monkeypatch) -> None:
    monkeypatch.setattr(
        cli_module,
        "make_designer_server",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("address already in use")),
    )

    result = CliRunner().invoke(cli, ["design"])

    assert result.exit_code == 1
    assert "failed to start designer: address already in use" in result.output
