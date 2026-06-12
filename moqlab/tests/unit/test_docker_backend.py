from __future__ import annotations

from pathlib import Path

from moqlab.orchestrator.docker_backend import (
    LABEL_NODE_ID,
    LABEL_ROLE,
    LABEL_RUN_ID,
    DockerBackend,
)


class _FakeContainer:
    def __init__(self, container_id: str, name: str, labels: dict[str, str]) -> None:
        self.id = container_id
        self.name = name
        self.labels = labels


class _FakeContainerCollection:
    def __init__(self, containers: list[_FakeContainer]) -> None:
        self._containers = containers
        self.list_calls: list[bool] = []

    def list(self, all: bool, filters: dict[str, str]):
        self.list_calls.append(all)
        run_id = filters["label"].split("=", 1)[1]
        return [
            container
            for container in self._containers
            if container.labels.get(LABEL_RUN_ID) == run_id
        ]


class _FakeNetwork:
    def __init__(self, network_id: str, name: str, run_id: str) -> None:
        self.id = network_id
        self.name = name
        self.attrs = {"Labels": {LABEL_RUN_ID: run_id}}


class _FakeNetworkCollection:
    def __init__(self, networks: list[_FakeNetwork]) -> None:
        self._networks = networks
        self.create_kwargs: dict[str, object] | None = None

    def list(self, filters: dict[str, str]):
        return self._networks

    def create(self, **kwargs):
        self.create_kwargs = kwargs
        return _FakeNetwork("net-new", kwargs["name"], kwargs["labels"][LABEL_RUN_ID])


class _FakeDockerClient:
    def __init__(
        self, networks: list[_FakeNetwork], containers: list[_FakeContainer]
    ) -> None:
        self.networks = _FakeNetworkCollection(networks)
        self.containers = _FakeContainerCollection(containers)


def _backend(client: _FakeDockerClient, tmp_path: Path) -> DockerBackend:
    backend = DockerBackend.__new__(DockerBackend)
    backend._client = client
    backend._runs_root = tmp_path
    return backend


def test_ls_omits_runs_with_no_running_containers(tmp_path: Path):
    client = _FakeDockerClient(
        networks=[_FakeNetwork("net-dead", "moqlab_dead", "dead-run")],
        containers=[],
    )

    assert _backend(client, tmp_path).ls() == []
    assert client.containers.list_calls == [False]


def test_ls_reports_runs_with_running_containers(tmp_path: Path):
    client = _FakeDockerClient(
        networks=[_FakeNetwork("net-active", "moqlab_active", "active-run")],
        containers=[
            _FakeContainer(
                "container-relay-a",
                "relay-a",
                {
                    LABEL_RUN_ID: "active-run",
                    LABEL_ROLE: "relay",
                    LABEL_NODE_ID: "relay-a",
                },
            )
        ],
    )

    records = _backend(client, tmp_path).ls()

    assert len(records) == 1
    assert records[0].run_id == "active-run"
    assert records[0].relays == {"relay-a": "container-relay-a"}
    assert client.containers.list_calls == [False]


def test_create_network_omits_deprecated_check_duplicate(tmp_path: Path):
    client = _FakeDockerClient(networks=[], containers=[])

    network = _backend(client, tmp_path)._create_network(
        "run-test", {LABEL_RUN_ID: "run-test"}
    )

    assert network.name == "moqlab_run-test"
    assert client.networks.create_kwargs is not None
    assert "check_duplicate" not in client.networks.create_kwargs
