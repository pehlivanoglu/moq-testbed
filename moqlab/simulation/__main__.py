from __future__ import annotations

import os
from pathlib import Path

import click

from moqlab.exceptions import MoqlabError
from simulation import runner
from simulation.schema import load_config, resolved_config


@click.group(help="QUIC/SBD discrete-event simulation. No Docker or Containernet.")
def cli() -> None:
    pass


@cli.command(help="Check simulation build tools and binary.")
def doctor() -> None:
    for name, ok, detail in runner.doctor():
        click.echo(f"{'OK' if ok else 'MISSING'} {name}: {detail}")


@cli.command(help="Fetch locked dependencies and build the simulator.")
@click.option("--jobs", type=click.IntRange(1, 64), default=min(8, os.cpu_count() or 1))
def build(jobs: int) -> None:
    runner.build(jobs)
    click.echo(str(runner.BINARY))


@cli.command(help="Validate topology, experiment settings, and resolved routes.")
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
def validate(config: Path) -> None:
    value = load_config(config)
    resolved_config(value)
    click.echo("valid simulation config")


@cli.command(help="Run all configured repetitions and archive results.")
@click.option("--config", "-c", required=True, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--output", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--jobs", type=click.IntRange(1, 32), default=1, help="Parallel independent repetitions.")
def run(config: Path, output: Path | None, jobs: int) -> None:
    click.echo(str(runner.run(load_config(config), config, output, jobs)))


@cli.command(help="Open the topology designer with simulation experiment controls.")
@click.option("--config", "-c", type=click.Path(exists=True, dir_okay=False, path_type=Path), default=None)
@click.option("--port", type=click.IntRange(1, 65535), default=8765)
def design(config: Path | None, port: int) -> None:
    from simulation.designer import make_server
    server = make_server(config, port)
    click.echo(f"simulation designer: http://127.0.0.1:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


@cli.command(help="Compute pairwise SBD scores and plots from archived runs.")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
def analyze(directory: Path) -> None:
    from simulation.analysis import analyze
    click.echo(str(analyze(directory)))


@cli.command(help="Run model, transport, SBD, and integration verification.")
def verify() -> None:
    from simulation.verification import verify
    click.echo(str(verify()))


if __name__ == "__main__":
    try:
        cli()
    except MoqlabError as error:
        click.echo(f"Error: {error}", err=True)
        raise SystemExit(1) from error
