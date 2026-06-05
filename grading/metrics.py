#!/usr/bin/env python3
"""
Compute aggregate metrics from benchmark results.

Metrics:
  - Solve Rate (SR) ± std over trials
  - False Flag Rate (FFR)
  - Knowledge Delta (KD) = SR(with_ctfbase) - SR(with_agents)
  - Agent Delta (AD) = SR(with_agents) - SR(bare)
  - Time to Solve (TTS)
  - Token Efficiency (TE)
  - Cost Efficiency (CE)
  - pass@1 (solved in at least 1 trial)
  - pass@n_avg (average solve rate)
  - Flaky Rate (solved in some but not all trials)
  - Per-category and per-difficulty breakdowns
  - CTF Score (difficulty-weighted with time bonus and cost penalty)

Usage:
  python grading/metrics.py --results results/20260503_pilot_results.json
"""

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

from scoring import compute_task_score, compute_leaderboard_scores  # noqa: E402


def compute_metrics(results: list[dict]) -> dict:
    """Compute all metrics from a list of result dicts."""
    
    # Group by (model, mode, task_id)
    groups = defaultdict(list)
    for r in results:
        key = (r["model"], r["mode"], r["task_id"])
        groups[key].append(r)

    # Group by (model, mode) for aggregate metrics
    model_mode_groups = defaultdict(list)
    for r in results:
        key = (r["model"], r["mode"])
        model_mode_groups[key].append(r)

    # Group by (model, mode, category) for per-category
    category_groups = defaultdict(list)
    for r in results:
        key = (r["model"], r["mode"], r.get("category", "unknown"))
        category_groups[key].append(r)

    # Group by (model, mode, difficulty) for per-difficulty
    difficulty_groups = defaultdict(list)
    for r in results:
        key = (r["model"], r["mode"], r.get("difficulty", "unknown"))
        difficulty_groups[key].append(r)

    metrics = {
        "per_model_mode": {},
        "per_task": {},
        "per_category": {},
        "per_difficulty": {},
        "deltas": {},
    }

    # Per (model, mode) aggregate metrics
    for (model, mode), runs in model_mode_groups.items():
        key = f"{model} | {mode}"

        solved = [r for r in runs if r.get("flag_correct")]
        wrong_flag = [r for r in runs if r.get("flag_found") and not r.get("flag_correct")]

        sr = len(solved) / len(runs) if runs else 0
        ffr = len(wrong_flag) / len(runs) if runs else 0

        # TTS (time to solve) — average time for solved tasks
        tts_values = [r["time_seconds"] for r in solved if "time_seconds" in r]
        tts = sum(tts_values) / len(tts_values) if tts_values else None

        # Token Efficiency — average tokens for solved
        te_values = [
            r.get("total_tokens_input", 0) + r.get("total_tokens_output", 0)
            for r in solved
        ]
        te = sum(te_values) / len(te_values) if te_values else None

        # Cost Efficiency — average cost for solved
        ce_values = [r.get("total_cost_usd", 0) for r in solved]
        ce = sum(ce_values) / len(ce_values) if ce_values else None

        # Total cost
        total_cost = sum(r.get("total_cost_usd", 0) for r in runs)

        # Cost per solve
        cost_per_solve = total_cost / len(solved) if solved else None

        metrics["per_model_mode"][key] = {
            "model": model,
            "mode": mode,
            "total_runs": len(runs),
            "solved": len(solved),
            "solve_rate": round(sr, 4),
            "false_flag_rate": round(ffr, 4),
            "tts_avg": round(tts, 1) if tts else None,
            "token_efficiency_avg": round(te) if te else None,
            "cost_efficiency_avg": round(ce, 4) if ce else None,
            "total_cost_usd": round(total_cost, 2),
            "cost_per_solve": round(cost_per_solve, 2) if cost_per_solve else None,
        }

    # Per (model, mode, task) — for pass@1, flaky, SR±std
    task_stats = defaultdict(lambda: defaultdict(list))
    for (model, mode, task_id), trials in groups.items():
        mm_key = f"{model} | {mode}"
        solved_trials = [1 if r.get("flag_correct") else 0 for r in trials]

        n = len(solved_trials)
        k = sum(solved_trials)
        sr = k / n if n else 0
        std = math.sqrt(sr * (1 - sr) / n) if n > 1 else 0

        pass_at_1 = 1 if k >= 1 else 0
        is_flaky = 1 if 0 < k < n else 0

        task_stats[mm_key][task_id] = {
            "trials": n,
            "solved": k,
            "sr": round(sr, 4),
            "std": round(std, 4),
            "pass_at_1": pass_at_1,
            "is_flaky": is_flaky,
        }

    # Compute aggregate pass@1 and flaky per model+mode
    for mm_key, tasks in task_stats.items():
        n_tasks = len(tasks)
        pass_at_1_count = sum(t["pass_at_1"] for t in tasks.values())
        flaky_count = sum(t["is_flaky"] for t in tasks.values())

        if mm_key in metrics["per_model_mode"]:
            metrics["per_model_mode"][mm_key]["pass_at_1"] = round(pass_at_1_count / n_tasks, 4) if n_tasks else 0
            metrics["per_model_mode"][mm_key]["flaky_rate"] = round(flaky_count / n_tasks, 4) if n_tasks else 0

            # SR with std across tasks
            sr_values = [t["sr"] for t in tasks.values()]
            mean_sr = sum(sr_values) / len(sr_values) if sr_values else 0
            if len(sr_values) > 1:
                variance = sum((s - mean_sr) ** 2 for s in sr_values) / len(sr_values)
                std_sr = math.sqrt(variance)
            else:
                std_sr = 0
            metrics["per_model_mode"][mm_key]["sr_mean"] = round(mean_sr, 4)
            metrics["per_model_mode"][mm_key]["sr_std"] = round(std_sr, 4)

    metrics["per_task"] = dict(task_stats)

    # Per category
    for (model, mode, category), runs in category_groups.items():
        key = f"{model} | {mode} | {category}"
        solved = sum(1 for r in runs if r.get("flag_correct"))
        sr = solved / len(runs) if runs else 0
        metrics["per_category"][key] = {
            "category": category,
            "total": len(runs),
            "solved": solved,
            "sr": round(sr, 4),
        }

    # Per difficulty
    for (model, mode, difficulty), runs in difficulty_groups.items():
        key = f"{model} | {mode} | {difficulty}"
        solved = sum(1 for r in runs if r.get("flag_correct"))
        sr = solved / len(runs) if runs else 0
        metrics["per_difficulty"][key] = {
            "difficulty": difficulty,
            "total": len(runs),
            "solved": solved,
            "sr": round(sr, 4),
        }

    # Deltas: for each model, compute AD and KD
    models = set(r["model"] for r in results)
    for model in models:
        bare_key = f"{model} | bare"
        agents_key = f"{model} | with_agents"
        ctfbase_key = f"{model} | with_ctfbase"

        bare_sr = metrics["per_model_mode"].get(bare_key, {}).get("solve_rate", 0)
        agents_sr = metrics["per_model_mode"].get(agents_key, {}).get("solve_rate", 0)
        ctfbase_sr = metrics["per_model_mode"].get(ctfbase_key, {}).get("solve_rate", 0)

        metrics["deltas"][model] = {
            "agent_delta": round(agents_sr - bare_sr, 4),
            "kb_delta": round(ctfbase_sr - agents_sr, 4),
            "total_delta": round(ctfbase_sr - bare_sr, 4),
        }

    # --- CTF Score ---
    # Compute per-result scores (use pre-computed task_score if present,
    # otherwise compute on the fly for backward compat with old results)
    for r in results:
        if "task_score" not in r:
            sc = compute_task_score(r)
            r["task_score"] = sc["task_score"]
            r["max_task_score"] = sc["max_task_score"]

    # Leaderboard scores (aggregated by model+mode)
    leaderboard = compute_leaderboard_scores(results)

    # Merge score data into per_model_mode
    for key, lb_data in leaderboard.items():
        if key in metrics["per_model_mode"]:
            metrics["per_model_mode"][key]["total_score"] = lb_data["total_score"]
            metrics["per_model_mode"][key]["max_possible_score"] = lb_data["max_possible"]
            metrics["per_model_mode"][key]["normalized_score"] = lb_data["normalized_score"]
            metrics["per_model_mode"][key]["avg_score_per_trial"] = lb_data["avg_score_per_trial"]
            metrics["per_model_mode"][key]["rank"] = lb_data["rank"]
            # Efficiency metrics (separate from solvability score)
            metrics["per_model_mode"][key]["points_per_dollar"] = lb_data.get("points_per_dollar")
            metrics["per_model_mode"][key]["points_per_min"] = lb_data.get("points_per_min")

    # Add per-task score details
    for mm_key, tasks in task_stats.items():
        for task_id, tdata in tasks.items():
            # Collect task_score values for this model+mode+task
            task_key = None
            for (model, mode, tid), trials in groups.items():
                if f"{model} | {mode}" == mm_key and tid == task_id:
                    scores = [r.get("task_score", 0) for r in trials]
                    tdata["avg_score"] = round(sum(scores) / len(scores), 1) if scores else 0
                    tdata["max_score"] = round(max(scores), 1) if scores else 0
                    break

    metrics["leaderboard"] = leaderboard

    return metrics


def main():
    parser = argparse.ArgumentParser(description="Compute benchmark metrics")
    parser.add_argument("--results", required=True, help="Path to results JSON")
    parser.add_argument("--output", default=None, help="Output metrics JSON path")
    args = parser.parse_args()

    with open(args.results) as f:
        results = json.load(f)

    metrics = compute_metrics(results)

    output = json.dumps(metrics, indent=2, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Metrics written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()
