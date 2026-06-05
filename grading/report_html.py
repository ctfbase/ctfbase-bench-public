#!/usr/bin/env python3
"""
Generate HTML dashboard from benchmark results.

Reads one or more results JSON files and produces a self-contained HTML report
with sortable tables, Chart.js graphs, deltas, per-task details, and history.

Usage:
  # Single run
  python grading/report_html.py --results results/pilot_v1_results.json

  # All runs in directory (aggregated dashboard with history)
  python grading/report_html.py --results-dir results/

  # Custom output path
  python grading/report_html.py --results results/pilot_v1_results.json -o report.html
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    print("ERROR: jinja2 not installed. Run: pip install jinja2")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
from metrics import compute_metrics  # noqa: E402


def load_results(results_path: Path) -> list[dict]:
    """Load a single results JSON file."""
    with open(results_path) as f:
        return json.load(f)


def load_results_dir(results_dir: Path) -> dict[str, list[dict]]:
    """Load all *_results.json from a directory, keyed by run_id."""
    runs = {}
    for f in sorted(results_dir.glob("*_results.json")):
        data = load_results(f)
        if data:
            run_id = data[0].get("run_id", f.stem)
            runs[run_id] = data
    return runs


def _result_quality(r: dict) -> tuple:
    """Ranking key for choosing the 'best' result among duplicates.

    Priority: solved > task_score > lower cost > faster.
    Higher tuple == better.
    """
    return (
        1 if r.get("flag_correct") else 0,
        r.get("task_score", 0) or 0,
        -(r.get("total_cost_usd", 0) or 0),
        -(r.get("time_seconds", 0) or 0),
    )


def dedupe_best(all_results: list[dict]) -> list[dict]:
    """Collapse duplicate (model, mode, task_id, trial_index) entries to the best one.

    When the same model+mode+task is executed across multiple separate runs,
    pooling their trials would mix runs and distort SR / pass@1. Instead we keep
    the single best outcome per (model, mode, task_id, trial_index) so each cell
    in the report reflects the best result rather than a merge of all runs.
    """
    best: dict[tuple, dict] = {}
    for r in all_results:
        key = (
            r.get("model"),
            r.get("mode"),
            r.get("task_id"),
            r.get("trial_index", 0),
        )
        cur = best.get(key)
        if cur is None or _result_quality(r) > _result_quality(cur):
            best[key] = r
    return list(best.values())


def build_task_detail(all_results: list[dict]) -> dict:
    """Build per-task detail data for the template."""
    by_task = defaultdict(lambda: {"runs": [], "solved": 0, "total": 0, "category": "", "difficulty": ""})

    for r in all_results:
        task_id = r.get("task_id", "unknown")
        by_task[task_id]["runs"].append(r)
        by_task[task_id]["total"] += 1
        by_task[task_id]["category"] = r.get("category", "")
        by_task[task_id]["difficulty"] = r.get("difficulty", "")
        if r.get("flag_correct"):
            by_task[task_id]["solved"] += 1

    # Sort runs within each task: by model, then mode, then trial
    for td in by_task.values():
        td["runs"].sort(key=lambda x: (x.get("model", ""), x.get("mode", ""), x.get("trial_index", 0)))

    return dict(by_task)


def build_head_to_head(all_results: list[dict], models: list[str], tasks: list[str]) -> dict:
    """Build head-to-head matrix: task × model → best SR across modes.

    Returns dict keyed by "task_id|model" with:
        {"best_sr": float, "best_solved": int, "best_total": int, "best_mode": str,
         "by_mode": {"bare": {"solved": int, "total": int}, ...}}
    """
    # Group by (task, model, mode)
    groups = defaultdict(lambda: {"solved": 0, "total": 0})
    for r in all_results:
        key = (r.get("task_id"), r.get("model"), r.get("mode"))
        groups[key]["total"] += 1
        if r.get("flag_correct"):
            groups[key]["solved"] += 1

    h2h = {}
    for task in tasks:
        for model in models:
            by_mode = {}
            best_sr = -1
            best_mode = ""
            best_solved = 0
            best_total = 0

            for mode in ["bare", "with_agents", "with_ctfbase"]:
                g = groups.get((task, model, mode))
                if g and g["total"] > 0:
                    sr = g["solved"] / g["total"]
                    by_mode[mode] = {"solved": g["solved"], "total": g["total"], "sr": round(sr, 4)}
                    if sr > best_sr:
                        best_sr = sr
                        best_mode = mode
                        best_solved = g["solved"]
                        best_total = g["total"]

            if best_total > 0:
                h2h[f"{task}|{model}"] = {
                    "best_sr": round(best_sr, 4),
                    "best_solved": best_solved,
                    "best_total": best_total,
                    "best_mode": best_mode,
                    "by_mode": by_mode,
                }

    return h2h


def build_leaderboard(models: list[str], mm: dict, deltas: dict) -> list[dict]:
    """Build one-row-per-(model, mode) leaderboard for the template.

    Each (model, mode) combination is its own row, so the same model run in
    multiple modes produces multiple rows. Repeated runs of the same
    (model, mode) are already collapsed to best-per-task upstream (dedupe_best).
    """
    mode_order = {"bare": 0, "with_agents": 1, "with_ctfbase": 2}
    rows = []
    for model in models:
        model_deltas = deltas.get(model, {})
        for mode in ["bare", "with_agents", "with_ctfbase"]:
            key = f"{model} | {mode}"
            d = mm.get(key)
            if not d:
                continue  # skip modes that were not run for this model
            row = {
                "model": model,
                "mode": mode,
                "short": model.split("/")[-1],
                "sr": d.get("solve_rate", 0),
                "solved": d.get("solved", 0),
                "total": d.get("total_runs", 0),
                "cost": d.get("total_cost_usd", 0),
                "cost_per_solve": d.get("cost_per_solve"),
                "tts": d.get("tts_avg"),
                "steps": d.get("avg_steps", 0),
                "score": d.get("normalized_score", 0),
                # Efficiency metrics (separate axis from solvability score)
                "pts_per_dollar": d.get("points_per_dollar"),
                "pts_per_min": d.get("points_per_min"),
                "rank": d.get("rank"),
                "mode_order": mode_order.get(mode, 9),
            }
            # Delta vs bare for this model (only meaningful for non-bare modes)
            if mode == "with_agents":
                row["delta"] = model_deltas.get("agent_delta", 0)
            elif mode == "with_ctfbase":
                row["delta"] = model_deltas.get("total_delta", 0)
            else:
                row["delta"] = None
            rows.append(row)
    return rows


def build_run_summaries(runs: dict[str, list[dict]]) -> dict:
    """Build summary cards per run_id."""
    summaries = {}
    for run_id, results in runs.items():
        solved = sum(1 for r in results if r.get("flag_correct"))
        total = len(results)
        cost = sum(r.get("total_cost_usd", 0) for r in results)
        models = sorted(set(r.get("model", "").split("/")[-1] for r in results))
        modes = sorted(set(r.get("mode", "") for r in results))
        tasks = sorted(set(r.get("task_id", "") for r in results))

        # Try to extract date from run_id (e.g., "20260503_pilot" -> "2026-05-03")
        date = run_id
        if len(run_id) >= 8 and run_id[:8].isdigit():
            date = f"{run_id[:4]}-{run_id[4:6]}-{run_id[6:8]}"

        summaries[run_id] = {
            "date": date,
            "models": models,
            "modes": modes,
            "tasks": tasks,
            "total_runs": total,
            "solved": solved,
            "sr": solved / total if total else 0,
            "cost": cost,
        }
    return summaries


def build_history(runs: dict[str, list[dict]], modes: list[str]) -> list[dict]:
    """Build history data: SR per run_id per mode."""
    history = []
    for run_id, results in runs.items():
        entry = {"run_id": run_id}
        for mode in modes:
            mode_results = [r for r in results if r.get("mode") == mode]
            if mode_results:
                solved = sum(1 for r in mode_results if r.get("flag_correct"))
                entry[mode] = solved / len(mode_results)
            else:
                entry[mode] = 0
        history.append(entry)
    return history


def add_avg_steps(per_model_mode: dict, all_results: list[dict]):
    """Add avg_steps to per_model_mode metrics."""
    groups = defaultdict(list)
    for r in all_results:
        key = f"{r.get('model', '')} | {r.get('mode', '')}"
        groups[key].append(r)

    for key, runs in groups.items():
        if key in per_model_mode:
            steps = [r.get("steps", 0) for r in runs if r.get("flag_correct")]
            per_model_mode[key]["avg_steps"] = (
                sum(steps) / len(steps) if steps else 0
            )


def generate_html(
    all_results: list[dict],
    runs: dict[str, list[dict]] | None = None,
    output_path: Path | None = None,
) -> str:
    """Generate HTML report from results."""
    # Collapse duplicate (model, mode, task, trial) across separate runs to the
    # best result, so repeated same-mode runs are not pooled/mixed in the
    # aggregate views (leaderboard, head-to-head, details, dashboard).
    # `runs` (per run_id) is kept untouched for the History section.
    all_results = dedupe_best(all_results)
    metrics = compute_metrics(all_results)

    # Extract dimensions
    models = sorted(set(r.get("model", "unknown") for r in all_results))
    modes_order = ["bare", "with_agents", "with_ctfbase"]
    modes = [m for m in modes_order if m in set(r.get("mode") for r in all_results)]
    tasks = sorted(set(r.get("task_id", "unknown") for r in all_results))
    categories = sorted(set(r.get("category", "unknown") for r in all_results))
    difficulties = [d for d in ["easy", "medium", "hard"] if d in set(r.get("difficulty") for r in all_results)]

    # Add avg_steps
    add_avg_steps(metrics["per_model_mode"], all_results)

    # Build per-task detail
    task_detail = build_task_detail(all_results)

    # Build run summaries
    if not runs:
        runs = {all_results[0].get("run_id", "run"): all_results}
    run_summaries = build_run_summaries(runs)

    # Build history
    history = build_history(runs, modes) if len(runs) > 1 else []

    # Build leaderboard (one row per model)
    leaderboard = build_leaderboard(models, metrics["per_model_mode"], metrics["deltas"])

    # Build head-to-head matrix (task × model)
    h2h = build_head_to_head(all_results, models, tasks)

    # Sort tasks by difficulty for head-to-head
    diff_order = {"easy": 0, "medium": 1, "hard": 2}
    tasks_sorted = sorted(tasks, key=lambda t: (
        diff_order.get(task_detail.get(t, {}).get("difficulty", "medium"), 1), t
    ))

    # Summary stats
    total_runs = len(all_results)
    overall_sr = sum(1 for r in all_results if r.get("flag_correct")) / total_runs if total_runs else 0
    total_cost = sum(r.get("total_cost_usd", 0) for r in all_results)

    # Render template
    env = Environment(
        loader=FileSystemLoader(str(SCRIPT_DIR / "templates")),
        autoescape=False,
    )
    template = env.get_template("report.html")

    html = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        total_runs=total_runs,
        overall_sr=overall_sr,
        total_cost=total_cost,
        models=models,
        modes=modes,
        tasks=tasks,
        tasks_sorted=tasks_sorted,
        categories=categories,
        difficulties=difficulties,
        run_summaries=run_summaries,
        leaderboard=leaderboard,
        h2h=h2h,
        mm=metrics["per_model_mode"],
        per_cat=metrics["per_category"],
        per_diff=metrics["per_difficulty"],
        deltas=metrics["deltas"],
        task_detail=task_detail,
        history=history,
    )

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(html, encoding="utf-8")
        print(f"Report written to {output_path}")

    return html


def main():
    parser = argparse.ArgumentParser(description="Generate HTML benchmark report")
    parser.add_argument("--results", default=None, help="Path to a single results JSON file")
    parser.add_argument("--results-dir", default=None, help="Path to directory with *_results.json files")
    parser.add_argument("-o", "--output", default=None, help="Output HTML path (default: results/report.html)")
    args = parser.parse_args()

    bench_dir = SCRIPT_DIR.parent

    if args.results:
        all_results = load_results(Path(args.results))
        runs = {all_results[0].get("run_id", "single"): all_results}
    elif args.results_dir:
        runs = load_results_dir(Path(args.results_dir))
        if not runs:
            print(f"No *_results.json found in {args.results_dir}")
            sys.exit(1)
        all_results = []
        for r in runs.values():
            all_results.extend(r)
    else:
        # Default: load all from results/
        results_dir = bench_dir / "results"
        runs = load_results_dir(results_dir)
        if not runs:
            print(f"No results found in {results_dir}")
            sys.exit(1)
        all_results = []
        for r in runs.values():
            all_results.extend(r)

    output = Path(args.output) if args.output else bench_dir / "results" / "report.html"
    generate_html(all_results, runs, output)


if __name__ == "__main__":
    main()
