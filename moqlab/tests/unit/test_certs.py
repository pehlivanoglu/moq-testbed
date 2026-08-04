from pathlib import Path

from moqlab.certs import generate_run_tls
from moqlab.config.schema import TopologyConfig


def test_generated_run_tls_uses_p256_sha256_and_secure_key(monkeypatch, tmp_path: Path):
    topology = TopologyConfig.model_validate(
        {
            "defaults": {"relay": {"tls": {"insecure": False, "generated": True}}},
            "relays": {"relay-a": {"listen_port": 9668, "admin_port": 9669}},
        }
    )
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        Path(argv[argv.index("-out") + 1]).touch()

    monkeypatch.setattr("moqlab.certs.subprocess.run", fake_run)
    tls_dir = generate_run_tls(topology, tmp_path)

    assert tls_dir == tmp_path / "tls"
    assert "prime256v1" in calls[0]
    assert "-sha256" in calls[1]
    assert "-days" in calls[1] and calls[1][calls[1].index("-days") + 1] == "13"
    assert "DNS:relay-a" in calls[1][calls[1].index("-addext") + 1]
    assert (tls_dir / "key.pem").stat().st_mode & 0o777 == 0o600
