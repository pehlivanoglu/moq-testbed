from __future__ import annotations

import bisect
import csv
import json
import os
import random
import hashlib
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from statistics import mean

from moqlab.exceptions import ConfigError


def rows(path: Path):
    with path.open() as stream:
        for line in stream:
            yield json.loads(line)


def ratios(counts: dict[str, int]) -> dict[str, float | None]:
    tp, fp, tn, fn = (counts[key] for key in ("tp", "fp", "tn", "fn"))
    def divide(a: float, b: float) -> float | None:
        return a / b if b else None
    return {"precision": divide(tp, tp + fp), "recall": divide(tp, tp + fn),
            "f1": divide(2 * tp, 2 * tp + fp + fn), "false_positive_rate": divide(fp, fp + tn),
            "coverage": divide(tp + fp + tn + fn, sum(counts.values()))}


def truth_pairs(value: dict[str, object]):
    if "pairs" in value:
        yield from value["pairs"]
        return
    paths = {key: set(path) for key, path in value["flow_paths"].items()}
    observed = {key: set(path) for key, path in value.get("observed_paths", value["flow_paths"]).items()}
    active = set(value["active_queues"])
    ids = sorted(paths)
    for index, a in enumerate(ids):
        for b in ids[index + 1:]:
            common = paths[a] & paths[b]
            yield {"a": a, "b": b, "structural": bool(common), "active": bool(observed.get(a, set()) & observed.get(b, set()) & active)}


def confidence(values: list[float], repetitions: int, seed: int) -> dict[str, object]:
    if not values:
        return {"mean": None, "ci95": None, "n": 0}
    if len(values) < 2:
        return {"mean": values[0], "ci95": None, "n": 1}
    rng = random.Random(seed)
    estimates = sorted(mean(rng.choices(values, k=len(values))) for _ in range(repetitions))
    return {"mean": mean(values), "ci95": [estimates[int(.025 * repetitions)], estimates[min(repetitions - 1, int(.975 * repetitions))]], "n": len(values)}


def score_run(directory: Path, active_override: dict[int, set[str]] | None = None) -> dict[str, object]:
    if not (directory / "COMPLETE").exists():
        raise ConfigError(f"refusing to score incomplete run: {directory}")
    reference = defaultdict(list)
    for item in rows(directory / "truth.jsonl"):
        if active_override is not None:
            item["active_queues"] = active_override.get(item["time_us"], set())
        reference[item["server"]].append(item)
    times = {server: [item["time_us"] for item in values] for server, values in reference.items()}
    counts = defaultdict(lambda: {"tp": 0, "fp": 0, "tn": 0, "fn": 0, "unknown": 0, "transition": 0})
    transitions = defaultdict(lambda: {"truth": None, "start": None, "pending": False})
    delays = defaultdict(list)
    missed_epochs = 0
    route_times = [item["time_us"] for item in rows(directory / "events.jsonl")
                   if item["event"] == "experiment_event" and item["parameters"]["kind"] == "route"]
    for snapshot in rows(directory / "sbd.jsonl"):
        server = snapshot["server"]
        known = {flow["id"] for flow in snapshot["flows"] if flow["known"]}
        grouped = {tuple(sorted((a, b))) for group in snapshot["groups"] for i, a in enumerate(group) for b in group[i + 1:]}
        for alignment, clock in [("evidence", snapshot["interval_end_us"]), ("decision", snapshot["time_us"])]:
            # Choose the interval containing the evidence watermark; never future to decision time.
            index = bisect.bisect_left(times.get(server, []), clock or snapshot["time_us"])
            if index >= len(reference[server]):
                continue
            truth = reference[server][index]
            if truth["time_us"] > snapshot["time_us"]:
                continue
            in_transition = any(truth["time_us"] - truth["window_us"] < event <= truth["time_us"] for event in route_times)
            for pair in truth_pairs(truth):
                identity = tuple(sorted((pair["a"], pair["b"])))
                prediction = identity in grouped
                for definition in ("structural", "active"):
                    key = f"{alignment}/{definition}"
                    count = counts[key]
                    if in_transition:
                        count["transition"] += 1
                        continue
                    if not clock or not set(identity).issubset(known):
                        count["unknown"] += 1
                        continue
                    actual = pair[definition]
                    count["tp" if prediction and actual else "fp" if prediction else "fn" if actual else "tn"] += 1
        # Operational onset/recovery latency includes warm-up; unknown decisions do not resolve it.
        truth_index = bisect.bisect_right(times.get(server, []), snapshot["time_us"]) - 1
        if truth_index < 0:
            continue
        for pair in truth_pairs(reference[server][truth_index]):
            identity = tuple(sorted((pair["a"], pair["b"])))
            state = transitions[server, identity]
            actual = pair["active"]
            if state["truth"] != actual:
                if state["pending"]:
                    missed_epochs += 1
                first = state["truth"] is None
                state.update(truth=actual, start=snapshot["time_us"], pending=actual or not first)
            prediction = identity in grouped
            if state["pending"] and set(identity).issubset(known) and prediction == actual:
                delays["detection_ms" if actual else "recovery_ms"].append((snapshot["time_us"] - state["start"]) / 1000)
                state["pending"] = False
    return {"scores": {key: {"counts": value, **ratios(value)} for key, value in sorted(counts.items())},
            "latency": {key: {"mean": mean(value), "samples": len(value)} for key, value in delays.items()},
            "missed_completed_epochs": missed_epochs,
            "unresolved_transitions": missed_epochs + sum(state["pending"] for state in transitions.values())}


def score_profiles(task: tuple[Path, dict[str, object]]) -> dict[str, object]:
    directory, evaluation = task
    result = score_run(directory)
    result["sensitivity"] = {}
    observations = None
    for factor in evaluation["sensitivity_factors"]:
        if factor == 1:
            continue
        if observations is None:
            observations = list(rows(directory / "queues.jsonl"))
        utilization = min(1, evaluation["utilization_threshold"] * factor)
        backlog = min(1, evaluation["backlog_fraction_threshold"] * factor)
        sojourn = evaluation["sojourn_threshold_ms"] * factor * 1000
        active = defaultdict(set)
        for value in observations:
            if value["utilization"] >= utilization and value["backlog_fraction"] >= backlog and value["mean_sojourn_us"] >= sojourn:
                active[value["time_us"]].add(value["queue"])
        profile = score_run(directory, active)
        result["sensitivity"][str(factor)] = {"utilization_threshold": utilization, "backlog_fraction_threshold": backlog,
                                              "sojourn_threshold_ms": sojourn / 1000, "scores": profile["scores"]}
    return result


def plot_run(directory: Path, destination: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    figure, axes = plt.subplots(3, 1, figsize=(11, 9), sharex=True)
    by_flow = defaultdict(list)
    for row in rows(directory / "applications.jsonl"):
        by_flow[row["flow"]].append(row)
    for flow, values in by_flow.items():
        if len(by_flow) > 12 and "client" not in flow:
            continue
        x = [v["time_us"] / 1e6 for v in values]
        axes[0].plot(x, [v.get("smoothed_rtt_us", 0) / 1000 for v in values], label=flow, alpha=.6)
    by_queue = defaultdict(list)
    for row in rows(directory / "queues.jsonl"):
        by_queue[row["queue"]].append(row)
    for queue, values in by_queue.items():
        if max(v["mean_sojourn_us"] for v in values) < 100:
            continue
        axes[1].plot([v["time_us"] / 1e6 for v in values], [v["mean_sojourn_us"] / 1000 for v in values], label=queue)
    by_server = defaultdict(list)
    for row in rows(directory / "sbd.jsonl"):
        by_server[row["server"]].append(row)
    for server, values in by_server.items():
        axes[2].step([v["time_us"] / 1e6 for v in values],
                     [sum(len(g) for g in v["groups"]) if sum(f["known"] for f in v["flows"]) >= 2 else float("nan") for v in values], label=server)
    axes[2].text(.01, .95, "Unknown/warm-up periods omitted", transform=axes[2].transAxes, va="top", fontsize=8)
    for ax, title in zip(axes, ["QUIC smoothed RTT (ms)", "Queue mean sojourn (ms)", "Flows in detected shared groups"]):
        ax.set_ylabel(title)
        ax.grid(alpha=.2)
        if ax.lines and len(ax.lines) <= 12:
            ax.legend(fontsize=7)
    axes[2].set_xlabel("Simulation time (s)")
    figure.tight_layout()
    figure.savefig(destination, dpi=140)
    plt.close(figure)


def analyze(directory: Path) -> Path:
    config_path = directory / "manifest.json"
    if not config_path.exists() or not (directory / "COMPLETE").exists():
        raise ConfigError("analysis requires a complete simulation archive")
    from simulation.schema import load_config
    config = load_config(directory / "resolved.yaml")
    evaluation = config.experiment.evaluation
    tasks = [(path, evaluation.model_dump()) for path in sorted(directory.glob("run-*")) if path.is_dir()]
    if len(tasks) > 1:
        with ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1)) as pool:
            results = list(pool.map(score_profiles, tasks))
    else:
        results = [score_profiles(task) for task in tasks]
    combined = {}
    for key in sorted({key for run in results for key in run["scores"]}):
        combined[key] = {}
        for metric in ("precision", "recall", "f1", "false_positive_rate", "coverage"):
            values = [run["scores"][key][metric] for run in results if key in run["scores"] and run["scores"][key][metric] is not None]
            combined[key][metric] = confidence(values, evaluation.bootstrap_samples, evaluation.analysis_seed)
    source = Path(__file__).read_bytes()
    (directory / "analysis-source.py").write_bytes(source)
    report = {"schema_version": 1, "algorithm": "PracticalPassiveAndRFC", "runs": results, "aggregate": combined,
              "analysis_source_sha256": hashlib.sha256(source).hexdigest(),
              "aggregate_latency": {metric: confidence([run["latency"][metric]["mean"] for run in results if metric in run["latency"]],
                                                       evaluation.bootstrap_samples, evaluation.analysis_seed)
                                    for metric in ("detection_ms", "recovery_ms")},
              "confidence_unit": "independent run", "unknown_policy": "excluded from confusion matrix; included in coverage denominator",
              "truth_definition": evaluation.model_dump(), "limits": ["Active congestion is an operational queue criterion, not a universal bottleneck oracle.",
                  "Ideal shared clock; timestamp feedback uses a dedicated reliable stream.",
                  "Internal validation does not establish physical-network equivalence."]}
    path = directory / "analysis.json"
    path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    columns = ["run", "reference", "tp", "fp", "tn", "fn", "unknown", "transition", "precision", "recall", "f1", "false_positive_rate", "coverage"]
    with (directory / "metrics.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for index, result in enumerate(results, 1):
            for reference, value in result["scores"].items():
                writer.writerow({"run": index, "reference": reference, **value["counts"],
                                 **{name: value[name] for name in columns[8:]}})
    plot_run(next(p for p in sorted(directory.glob("run-*")) if p.is_dir()), directory / "overview.png")
    return path
