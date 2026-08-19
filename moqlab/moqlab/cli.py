from __future__ import annotations

import atexit
import sys
import threading
import time
from pathlib import Path

import click

from moqlab.build import (
    docker_image_build_commands,
    media_image_build_commands,
    moqx_build_commands,
    run_build_command,
)
from moqlab.cleanup import remove_pycaches
from moqlab.config.schema import load_topology
from moqlab.doctor import doctor_checks, ensure_run_ready, has_failures
from moqlab.designer import DesignerHTTPServer, make_designer_server
from moqlab.exceptions import MoqlabError
from moqlab.orchestrator.docker_backend import DockerBackend
from moqlab.visualizer import VisualizerHTTPServer, make_server

_VIS_HOST = "127.0.0.1"
_VIS_PORT = 8765


@click.group(
    help=(
        "Run MoQ relay topologies from YAML. `run` defaults to the "
        "Containernet backend; use `build` to prepare binaries/images first."
    )
)
@click.option(
    "--runs-dir",
    type=click.Path(file_okay=False, path_type=Path),
    envvar="MOQLAB_RUNS_DIR",
    default=None,
    help="Directory holding per-run state. Defaults to moqlab/.runs/.",
)
@click.pass_context
def cli(ctx: click.Context, runs_dir: Path | None) -> None:
    ctx.ensure_object(dict)
    ctx.obj["runs_dir"] = runs_dir


def _docker_backend(ctx: click.Context) -> DockerBackend:
    try:
        return DockerBackend(runs_dir=ctx.obj.get("runs_dir"))
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e


@cli.command(help="Open the local drag-and-drop topology YAML designer.")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional topology YAML to load into the editor.",
)
@click.option(
    "--port",
    type=click.IntRange(1, 65535),
    default=_VIS_PORT,
    show_default=True,
)
def design(config: Path | None, port: int) -> None:
    server: DesignerHTTPServer | None = None
    try:
        server = make_designer_server(host=_VIS_HOST, port=port, config_path=config)
        click.echo(f"designer: http://{_VIS_HOST}:{server.server_address[1]}/")
        click.echo("press Ctrl-C to stop")
        server.serve_forever()
    except KeyboardInterrupt:
        click.echo("\nstopped designer")
    except (MoqlabError, OSError) as error:
        raise click.ClickException(f"failed to start designer: {error}") from error
    finally:
        if server is not None:
            server.server_close()


@cli.group(help="Prepare local build artifacts. Build commands do not run topologies.")
def build() -> None:
    pass


@build.command(
    "moqx",
    help="Build moqx and prepare the moxygen binaries copied into node images.",
)
def build_moqx() -> None:
    try:
        commands = moqx_build_commands()
        for command in commands:
            click.echo(f"==> {command.label}")
            click.echo(f"    {' '.join(command.argv)}")
            run_build_command(command)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e


@build.command(
    "images",
    help="Build relay, router, and external-traffic images.",
)
def build_images() -> None:
    try:
        commands = docker_image_build_commands()
        for command in commands:
            click.echo(f"==> {command.label}")
            click.echo(f"    {' '.join(command.argv)}")
            run_build_command(command)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e


@build.command(
    "media-images",
    help="Build AV1-SVC publisher, Chrome, and native subscriber images.",
)
@click.option(
    "--publisher-context",
    type=click.Path(file_okay=False, path_type=Path),
    envvar="MOQLAB_MEDIA_PUBLISHER_CONTEXT",
    default=None,
)
@click.option(
    "--player-context",
    type=click.Path(file_okay=False, path_type=Path),
    envvar="MOQLAB_MEDIA_PLAYER_CONTEXT",
    default=None,
)
def build_media_images(
    publisher_context: Path | None, player_context: Path | None
) -> None:
    try:
        commands = media_image_build_commands(
            publisher_context=publisher_context,
            player_context=player_context,
        )
        for command in commands:
            click.echo(f"==> {command.label}")
            click.echo(f"    {' '.join(command.argv)}")
            run_build_command(command)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e


@cli.group(name="rm", help="Remove generated local development artifacts.")
def remove() -> None:
    pass


@remove.command(
    "pycaches",
    help="Remove __pycache__, .pyc/.pyo, and .pytest_cache under moqlab/.",
)
def remove_pycache_files() -> None:
    _remove_pycaches_command()


@remove.command("pycache", hidden=True)
def remove_pycache_files_alias() -> None:
    _remove_pycaches_command()


def _remove_pycaches_command() -> None:
    project_root = Path(__file__).resolve().parents[1]
    result = remove_pycaches(project_root)
    # This CLI invocation imports moqlab modules, so CPython can recreate
    # __pycache__ after the cleanup pass. Re-run at process exit to leave the
    # tree clean for callers that use this command in scripts.
    atexit.register(remove_pycaches, project_root)
    click.echo(
        "removed "
        f"{result.pycache_dirs} __pycache__ dirs and "
        f"{result.bytecode_files} bytecode files and "
        f"{result.pytest_cache_dirs} .pytest_cache dirs"
    )


@cli.command(help="Check Python deps, Docker, images, Containernet, and config readiness.")
@click.option(
    "--config",
    "-c",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Optional topology config. When set, checks the images named by that config.",
)
@click.option(
    "--backend",
    type=click.Choice(["docker", "containernet"], case_sensitive=False),
    default="containernet",
    show_default=True,
    help="Backend to check. Containernet adds Mininet import and privilege checks.",
)
def doctor(config: Path | None, backend: str) -> None:
    checks = doctor_checks(config_path=config, backend=backend)
    click.echo(_doctor_summary(checks))
    current_category: str | None = None
    for check in checks:
        if check.category != current_category:
            current_category = check.category
            click.echo(f"\n{current_category}")
        click.echo(f"  {check.status.upper():4} {check.name}: {check.message}")
        if check.next_step:
            click.echo(f"       next: {check.next_step}")
    if has_failures(checks):
        raise click.exceptions.Exit(1)


def _doctor_summary(checks) -> str:
    failures = sum(1 for check in checks if check.status == "fail")
    warnings = sum(1 for check in checks if check.status == "warn")
    if failures:
        return f"moqlab doctor: not ready ({failures} failed, {warnings} warnings)"
    if warnings:
        return f"moqlab doctor: ready with warnings ({warnings} warnings)"
    return "moqlab doctor: ready"


@cli.command(help="Parse and validate a topology config without side effects.")
@click.option(
    "--config",
    "-c",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def validate(config: Path) -> None:
    try:
        topology = load_topology(config)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e

    click.echo(f"OK: {config}")
    click.echo(
        f"  relays={len(topology.relays)} "
        f"routers={len(topology.routers)} "
        f"publishers={len(topology.publishers)} "
        f"subscribers={len(topology.subscribers)} "
        f"traffic_endpoints={2 if topology.traffic is not None else 0} "
        f"links={len(topology.links)}"
    )
    for rid, r in topology.relays.items():
        up = r.upstream or "<origin>"
        click.echo(
            f"  relay      {rid:14}  listen={r.listen_port}  "
            f"admin={r.admin_port}  upstream={up}"
        )
    for rid in topology.routers:
        click.echo(f"  router     {rid:14}  image={topology.router_image(rid)}")
    for pid, p in topology.publishers.items():
        click.echo(f"  publisher  {pid:14}  -> {p.connects_to}  asset={p.asset}")
    for sid, s in topology.subscribers.items():
        click.echo(
            f"  subscriber {sid:14}  -> {s.connects_to}  ns={s.namespace}  "
            f"track={s.track}  client={topology.subscriber_media_client(sid)}"
        )
    if topology.traffic is not None:
        click.echo(
            f"  traffic     {topology.traffic.sender.id:14}  sender  "
            f"image={topology.traffic_image(topology.traffic.sender.id)}"
        )
        click.echo(
            f"  traffic     {topology.traffic.receiver.id:14}  receiver  "
            f"image={topology.traffic_image(topology.traffic.receiver.id)}"
        )


def _run_options(default_backend: str):
    def _decorator(fn):
        fn = click.option(
            "--vis",
            "--visualize",
            "visualize",
            is_flag=True,
            help="Serve a localhost topology and throughput visualizer during the run.",
        )(fn)
        fn = click.option(
            "--readiness-timeout",
            type=float,
            default=10.0,
            show_default=True,
            help="Docker backend only: per-container startup timeout (seconds).",
        )(fn)
        fn = click.option(
            "--publish-ports/--no-publish-ports",
            default=False,
            help="Docker backend only: publish each relay's ports on the host.",
        )(fn)
        fn = click.option(
            "--run-id",
            default=None,
            help="Run identifier. Auto-generated if omitted.",
        )(fn)
        fn = click.option(
            "--backend",
            type=click.Choice(["docker", "containernet"], case_sensitive=False),
            default=default_backend,
            show_default=True,
            help=(
                "Orchestration backend. Default containernet opens a Mininet CLI "
                "and tears down on exit; docker runs detached."
            ),
        )(fn)
        fn = click.option(
            "--config",
            "-c",
            required=True,
            type=click.Path(exists=True, dir_okay=False, path_type=Path),
        )(fn)
        return click.pass_context(fn)

    return _decorator


@cli.command(
    name="run",
    help=(
        "Run the topology described by CONFIG. Defaults to containernet. "
        "Use `moqlab doctor -c CONFIG` when readiness fails."
    ),
)
@_run_options(default_backend="containernet")
def run_topology(
    ctx: click.Context,
    config: Path,
    backend: str,
    run_id: str | None,
    publish_ports: bool,
    readiness_timeout: float,
    visualize: bool,
) -> None:
    _run_topology(
        ctx,
        config,
        backend,
        run_id,
        publish_ports,
        readiness_timeout,
        visualize,
    )

def _run_topology(
    ctx: click.Context,
    config: Path,
    backend: str,
    run_id: str | None,
    publish_ports: bool,
    readiness_timeout: float,
    visualize: bool,
) -> None:
    backend = backend.lower()
    try:
        ensure_run_ready(config, backend)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e

    visualizer = _start_visualizer(config, backend) if visualize else None
    if backend == "docker":
        _up_docker(
            ctx,
            config,
            run_id,
            publish_ports,
            readiness_timeout,
            visualizer,
        )
    elif backend == "containernet":
        _up_containernet(ctx, config, run_id, visualizer)
    else:
        _stop_visualizer(visualizer)
        raise click.ClickException(f"unknown backend: {backend}")


def _up_docker(
    ctx: click.Context,
    config: Path,
    run_id: str | None,
    publish_ports: bool,
    readiness_timeout: float,
    visualizer: VisualizerHTTPServer | None,
) -> None:
    try:
        backend = _docker_backend(ctx)
        record = backend.up(
            config_path=config,
            run_id=run_id,
            publish_ports=publish_ports,
            readiness_timeout_s=readiness_timeout,
        )
    except MoqlabError as e:
        _stop_visualizer(visualizer)
        raise click.ClickException(str(e)) from e

    click.echo(f"run_id:   {record.run_id}")
    click.echo(f"network:  {record.network_name}")
    click.echo(f"run_dir:  {record.run_dir}")
    if visualizer is not None:
        visualizer.register_subscriber_containers(record.subscribers)
    if record.relays:
        click.echo("relays:")
        for rid, cid in record.relays.items():
            click.echo(f"  {rid:14}  {cid[:12]}")
    if record.publishers:
        click.echo("publishers:")
        for pid, cid in record.publishers.items():
            click.echo(f"  {pid:14}  {cid[:12]}")
    if record.subscribers:
        click.echo("subscribers:")
        for sid, cid in record.subscribers.items():
            click.echo(f"  {sid:14}  {cid[:12]}")
    click.echo(f"\nlogs:  moqlab logs --run-id {record.run_id} <node_id>")
    click.echo(f"down:  moqlab down --run-id {record.run_id}")
    if visualizer is not None:
        _wait_for_visualizer_interrupt(visualizer)


def _up_containernet(
    ctx: click.Context,
    config: Path,
    run_id: str | None,
    visualizer: VisualizerHTTPServer | None,
) -> None:
    from moqlab.orchestrator.containernet_backend import ContainernetBackend

    try:
        cn = ContainernetBackend(runs_dir=ctx.obj.get("runs_dir"))
        record = cn.up(config_path=config, run_id=run_id)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e
    finally:
        _stop_visualizer(visualizer)
    click.echo(f"\ntorn down: run_id={record.run_id} (run dir kept at {record.run_dir})")


def _start_visualizer(config: Path, backend: str) -> VisualizerHTTPServer:
    try:
        server = make_server(
            config_path=config,
            host=_VIS_HOST,
            port=_VIS_PORT,
            backend=backend,
        )
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e
    except OSError as e:
        raise click.ClickException(f"failed to start visualizer: {e}") from e

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    click.echo(f"visualizer: http://{host}:{port}/")
    click.echo("throughput: live per-link rates require an active Containernet run")
    return server


def _stop_visualizer(server: VisualizerHTTPServer | None) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()


def _wait_for_visualizer_interrupt(server: VisualizerHTTPServer) -> None:
    click.echo("visualizer is running; press Ctrl-C to stop it")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        _stop_visualizer(server)
        click.echo("\nstopped visualizer")


@cli.command(help="Tear down a Docker-backend topology.")
@click.option("--run-id", required=True)
@click.pass_context
def down(ctx: click.Context, run_id: str) -> None:
    backend = _docker_backend(ctx)
    try:
        backend.down(run_id)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"stopped: {run_id}")


@cli.command(help="List active Docker-backend runs from Docker labels.")
@click.pass_context
def ls(ctx: click.Context) -> None:
    backend = _docker_backend(ctx)
    records = backend.ls()
    if not records:
        click.echo("(no active runs)")
        return
    for r in records:
        total = len(r.relays) + len(r.publishers) + len(r.subscribers)
        click.echo(
            f"{r.run_id}  net={r.network_name}  "
            f"nodes={total} (relays={len(r.relays)}, pubs={len(r.publishers)}, subs={len(r.subscribers)})"
        )


@cli.command(help="Print container logs for one node in a Docker-backend run.")
@click.option("--run-id", required=True)
@click.option("-f", "--follow", is_flag=True, help="Stream logs as they arrive.")
@click.option(
    "-n",
    "--tail",
    default=200,
    show_default=True,
    help="Lines to print from the tail before streaming.",
)
@click.argument("node_id")
@click.pass_context
def logs(ctx: click.Context, run_id: str, node_id: str, follow: bool, tail: int) -> None:
    backend = _docker_backend(ctx)
    try:
        container = backend.container_for(run_id, node_id)
    except MoqlabError as e:
        raise click.ClickException(str(e)) from e

    stream = container.logs(stream=follow, follow=follow, tail=tail)
    if follow:
        try:
            for chunk in stream:
                sys.stdout.buffer.write(chunk)
                sys.stdout.flush()
        except KeyboardInterrupt:
            return
    else:
        sys.stdout.buffer.write(stream)
        sys.stdout.flush()


if __name__ == "__main__":
    cli()
