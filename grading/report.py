#!/usr/bin/env python3
"""
Generate markdown report tables from benchmark metrics.

Usage:
  python grading/report.py --results results/20260503_pilot_results.json
  python grading/report.py --results results/20260503_pilot_results.json --output results/report.md
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import compute_metrics  # noqa: E402


def format_sr(sr: float, std: float, n: int) -> str:
    """Format solve rate with std and trial count."""
    return f"{sr*100:.0f}% ± {std*100:.0f} ({n})"


def format_pct(value: float) -> str:
    """Format percentage."""
    if value is None:
        return "—"
    return f"{value*100:.0f}%"


def format_delta(value: float) -> str:
    """Format delta with sign."""
    if value is None:
        return "—"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value*100:.0f}%"


def format_cost(value: float | None) -> str:
    if value is None:
        return "—"
    return f"${value:.2f}"


def generate_report(results: list[dict], run_id: str = "") -> str:
    """Generate markdown report from results."""
    metrics = compute_metrics(results)

    lines = []
    date = datetime.now().strftime("%Y-%m-%d")
    lines.append(f"# CTFBase-Bench Results — {run_id or date}\n")

    # Overall Solve Rate table
    lines.append("## Overall Solve Rate\n")

    # Collect models and modes
    models = sorted(set(r["model"] for r in results))
    modes = ["bare", "with_agents", "with_ctfbase"]
    available_modes = sorted(set(r["mode"] for r in results))
    modes = [m for m in modes if m in available_modes]

    # Header
    header = "| Model |"
    separator = "|-------|"
    for mode in modes:
        header += f" {mode} |"
        separator += "------|"
    header += " Agent Delta | KB Delta |"
    separator += "-------------|----------|"
    lines.append(header)
    lines.append(separator)

    for model in models:
        row = f"| {model.split('/')[-1]} |"
        for mode in modes:
            key = f"{model} | {mode}"
            data = metrics["per_model_mode"].get(key, {})
            sr = data.get("sr_mean", data.get("solve_rate", 0))
            std = data.get("sr_std", 0)
            n = data.get("total_runs", 0)
            row += f" {format_sr(sr, std, n)} |"

        deltas = metrics["deltas"].get(model, {})
        row += f" {format_delta(deltas.get('agent_delta'))} |"
        row += f" {format_delta(deltas.get('kb_delta'))} |"
        lines.append(row)

    lines.append("")

    # pass@1
    lines.append("## pass@1 (solved in at least 1 trial)\n")
    header = "| Model |"
    separator = "|-------|"
    for mode in modes:
        header += f" {mode} |"
        separator += "------|"
    lines.append(header)
    lines.append(separator)

    for model in models:
        row = f"| {model.split('/')[-1]} |"
        for mode in modes:
            key = f"{model} | {mode}"
            data = metrics["per_model_mode"].get(key, {})
            row += f" {format_pct(data.get('pass_at_1'))} |"
        lines.append(row)

    lines.append("")

    # False Flag Rate
    lines.append("## False Flag Rate\n")
    header = "| Model |"
    separator = "|-------|"
    for mode in modes:
        header += f" {mode} |"
        separator += "------|"
    lines.append(header)
    lines.append(separator)

    for model in models:
        row = f"| {model.split('/')[-1]} |"
        for mode in modes:
            key = f"{model} | {mode}"
            data = metrics["per_model_mode"].get(key, {})
            row += f" {format_pct(data.get('false_flag_rate'))} |"
        lines.append(row)

    lines.append("")

    # By Category (with_ctfbase or last available mode)
    best_mode = modes[-1] if modes else "bare"
    lines.append(f"## By Category ({best_mode} mode)\n")

    categories = sorted(set(r.get("category", "unknown") for r in results))
    header = "| Model |"
    separator = "|-------|"
    for cat in categories:
        header += f" {cat} |"
        separator += "------|"
    lines.append(header)
    lines.append(separator)

    for model in models:
        row = f"| {model.split('/')[-1]} |"
        for cat in categories:
            key = f"{model} | {best_mode} | {cat}"
            data = metrics["per_category"].get(key, {})
            row += f" {format_pct(data.get('sr'))} |"
        lines.append(row)

    lines.append("")

    # By Difficulty
    lines.append(f"## By Difficulty ({best_mode} mode)\n")

    difficulties = ["easy", "medium", "hard"]
    available_diffs = sorted(set(r.get("difficulty", "unknown") for r in results))
    difficulties = [d for d in difficulties if d in available_diffs]

    header = "| Model |"
    separator = "|-------|"
    for diff in difficulties:
        header += f" {diff} |"
        separator += "------|"
    lines.append(header)
    lines.append(separator)

    for model in models:
        row = f"| {model.split('/')[-1]} |"
        for diff in difficulties:
            key = f"{model} | {best_mode} | {diff}"
            data = metrics["per_difficulty"].get(key, {})
            row += f" {format_pct(data.get('sr'))} |"
        lines.append(row)

    lines.append("")

    # Cost Analysis
    lines.append("## Cost Analysis\n")
    lines.append("| Model | Mode | Avg cost/task | Cost/solve | Total cost |")
    lines.append("|-------|------|---------------|------------|------------|")

    for model in models:
        for mode in modes:
            key = f"{model} | {mode}"
            data = metrics["per_model_mode"].get(key, {})
            if not data:
                continue
            row = f"| {model.split('/')[-1]} | {mode}"
            row += f" | {format_cost(data.get('cost_efficiency_avg'))}"
            row += f" | {format_cost(data.get('cost_per_solve'))}"
            row += f" | {format_cost(data.get('total_cost_usd'))}"
            row += " |"
            lines.append(row)

    lines.append("")

    # --- CTF Score Leaderboard ---
    lines.append("## Leaderboard (CTF Score)\n")
    lines.append("Score = base_points (easy=100, medium=200, hard=400)")
    lines.append("      x time_bonus (up to +50% for fast solve)")
    lines.append("      x cost_multiplier (penalty for expensive runs)\n")

    # Build rows sorted by normalized_score
    lb_rows = []
    for model in models:
        for mode in modes:
            key = f"{model} | {mode}"
            data = metrics["per_model_mode"].get(key, {})
            if not data:
                continue
            lb_rows.append({
                "model": model.split("/")[-1],
                "mode": mode,
                "score": data.get("normalized_score", 0),
                "sr": data.get("solve_rate", 0),
                "tts": data.get("tts_avg"),
                "total_cost": data.get("total_cost_usd", 0),
                "rank": data.get("rank", 99),
            })
    lb_rows.sort(key=lambda x: x["rank"])

    lines.append("| Rank | Model | Mode | Score /1000 | SR | Avg TTS | Total Cost |")
    lines.append("|------|-------|------|-------------|-----|---------|------------|")

    for r in lb_rows:
        tts_str = f"{r['tts']:.0f}s" if r["tts"] else "—"
        lines.append(
            f"| {r['rank']} | {r['model']} | {r['mode']}"
            f" | {r['score']:.0f} | {r['sr']*100:.0f}%"
            f" | {tts_str} | {format_cost(r['total_cost'])} |"
        )

    lines.append("")

    # --- Per-Task Score ---
    per_task = metrics.get("per_task", {})
    if per_task:
        lines.append("## Per-Task Score\n")

        # Collect all (model, mode) keys and all task_ids
        mm_keys = sorted(per_task.keys())
        all_task_ids = sorted(
            set(tid for tasks in per_task.values() for tid in tasks.keys())
        )

        # Build a header with task difficulty
        task_diffs = {}
        for r in results:
            task_diffs[r["task_id"]] = r.get("difficulty", "?")

        lines.append("| Model / Mode | " + " | ".join(
            f"{tid} ({task_diffs.get(tid, '?')[0]})" for tid in all_task_ids
        ) + " | Total |")
        lines.append("|" + "------|" * (len(all_task_ids) + 2))

        for mm_key in mm_keys:
            tasks = per_task[mm_key]
            short_name = mm_key.split("|")[-1].strip() if "|" in mm_key else mm_key
            model_part = mm_key.split("|")[0].strip().split("/")[-1]
            label = f"{model_part} / {short_name}"

            cells = []
            total_avg = 0
            for tid in all_task_ids:
                tdata = tasks.get(tid, {})
                avg_s = tdata.get("avg_score", 0)
                total_avg += avg_s
                sr = tdata.get("sr", 0)
                if sr > 0:
                    cells.append(f"{avg_s:.0f} ({sr*100:.0f}%)")
                else:
                    cells.append("0")
            cells.append(f"**{total_avg:.0f}**")
            lines.append(f"| {label} | " + " | ".join(cells) + " |")

        lines.append("")

    lines.append("## Note on Cybench Comparison\n")
    lines.append("Direct comparison with Cybench is not the goal. Our distribution is web-heavy")
    lines.append("to test KB leverage. Use SR by category to compare per-domain capability.")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Generate benchmark report")
    parser.add_argument("--results", required=True, help="Path to results JSON")
    parser.add_argument("--output", default=None, help="Output markdown path")
    args = parser.parse_args()

    with open(args.results) as f:
        results = json.load(f)

    run_id = results[0].get("run_id", "") if results else ""
    report = generate_report(results, run_id)

    if args.output:
        Path(args.output).write_text(report)
        print(f"Report written to {args.output}")
    else:
        print(report)


if __name__ == "__main__":
    main()
