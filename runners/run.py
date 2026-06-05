#!/usr/bin/env python3
"""
Unified benchmark entry point: prepare + run + report in one command.

Minimal usage:
  python3 runners/run.py --models anthropic/claude-sonnet-4

With custom agents:
  python3 runners/run.py --models anthropic/claude-sonnet-4 --agents-dir agents/custom

With CTFBase knowledge base:
  python3 runners/run.py --models anthropic/claude-sonnet-4 --agents-dir agents/custom --ctfbase

Compare bare vs custom agents:
  python3 runners/run.py --models anthropic/claude-sonnet-4 --agents-dir agents/custom --modes bare,custom
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import yaml

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR / "runners"))
sys.path.insert(0, str(BENCH_DIR / "grading"))

from preflight import preflight  # noqa: E402
from prepare import prepare  # noqa: E402
from runner import install_shutdown_handler, run  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        description="CTFBase-Bench — CTF agent benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  # Quick test — one easy task
  python3 runners/run.py --models anthropic/claude-sonnet-4 --tasks dynastic --trials 1

  # Full run with your agents
  python3 runners/run.py --models anthropic/claude-sonnet-4 --agents-dir agents/custom --parallel 3

  # Compare bare model vs your agents
  python3 runners/run.py --models anthropic/claude-sonnet-4 --agents-dir agents/custom --modes bare,custom

  # Multi-model comparison
  python3 runners/run.py --models anthropic/claude-sonnet-4,openai/o3 --trials 5 --parallel 4
""",
    )

    parser.add_argument(
        "--models", required=True,
        help="Comma-separated OpenRouter model IDs (required)",
    )
    parser.add_argument(
        "--agents-dir", default=None,
        help="Path to directory with agent .md files (e.g., agents/custom)",
    )
    parser.add_argument(
        "--ctfbase", action="store_true",
        help="Enable CTFBase MCP knowledge base (requires CTFBASE_API_KEY)",
    )
    parser.add_argument(
        "--modes", default=None,
        help="Comma-separated modes: bare, custom (default: auto-detected from --agents-dir)",
    )
    parser.add_argument(
        "--tasks", default=None,
        help="Comma-separated task IDs (default: all)",
    )
    parser.add_argument(
        "--trials", type=int, default=3,
        help="Number of trials per cell (default: 3)",
    )
    parser.add_argument(
        "--parallel", type=int, default=1,
        help="Number of parallel Docker containers (default: 1)",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="Run identifier (default: auto-generated from timestamp)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print plan without running containers",
    )
    parser.add_argument(
        "--no-report", action="store_true",
        help="Skip HTML report generation after run",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt (for CI/automation)",
    )

    args = parser.parse_args()

    # Parse lists
    models = [m.strip() for m in args.models.split(",")]
    tasks = [t.strip() for t in args.tasks.split(",")] if args.tasks else None

    # Determine modes
    if args.modes:
        modes = [m.strip() for m in args.modes.split(",")]
    elif args.agents_dir:
        modes = ["bare", "custom"]
    else:
        modes = ["bare"]

    # Auto-generate run-id
    run_id = args.run_id or f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    num_tasks = len(tasks) if tasks else 10
    total_cells = len(models) * len(modes) * num_tasks * args.trials
    print("CTFBase-Bench", flush=True)
    print(f"  Run ID:   {run_id}", flush=True)
    print(f"  Models:   {', '.join(models)}", flush=True)
    print(f"  Modes:    {', '.join(modes)}", flush=True)
    if args.agents_dir:
        print(f"  Agents:   {args.agents_dir}", flush=True)
    if args.ctfbase:
        print(f"  CTFBase:  enabled", flush=True)
    print(f"  Tasks:    {', '.join(tasks) if tasks else 'all'}", flush=True)
    print(f"  Trials:   {args.trials}", flush=True)
    print(f"  Parallel: {args.parallel}", flush=True)
    print(f"  Total:    {total_cells} runs", flush=True)
    print(flush=True)

    wall_start = time.time()

    # Step 1: Prepare workdirs
    try:
        manifest_path = prepare(
            run_id=run_id,
            models=models,
            modes=modes,
            tasks=tasks,
            trials=args.trials,
            agents_dir=args.agents_dir,
            ctfbase=args.ctfbase,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR (prepare): {e}", flush=True)
        sys.exit(1)

    # Step 1.5: Pre-flight checks
    with open(manifest_path) as f:
        manifest = json.load(f)
    with open(BENCH_DIR / "config.yaml") as f:
        config = yaml.safe_load(f)

    preflight(manifest, config, skip_confirm=args.yes or args.dry_run)

    # Step 2: Run benchmark
    install_shutdown_handler()
    results_path = BENCH_DIR / "results" / f"{run_id}_results.json"

    try:
        results_path = run(
            manifest_path=manifest_path,
            parallel=args.parallel,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\nInterrupted. Partial results saved.", flush=True)
    except (EnvironmentError, RuntimeError) as e:
        print(f"ERROR (runner): {e}", flush=True)
        sys.exit(1)

    wall_elapsed = time.time() - wall_start

    if args.dry_run:
        print(f"\nDry run complete in {wall_elapsed:.0f}s.", flush=True)
        return

    # Step 3: Generate HTML reports
    if not args.no_report and results_path.exists():
        try:
            from report_html import generate_html, load_results_dir  # noqa: E402

            with open(results_path) as f:
                all_results = json.load(f)

            report_path = results_path.with_suffix(".html")
            generate_html(all_results, output_path=report_path)
            print(f"\nHTML report: {report_path}", flush=True)

            # Combined report if multiple runs exist
            results_dir = BENCH_DIR / "results"
            all_runs = {
                k: v for k, v in load_results_dir(results_dir).items()
                if k.startswith("run_")
            }
            if len(all_runs) > 1:
                combined = []
                for r in all_runs.values():
                    combined.extend(r)
                combined_path = results_dir / "report.html"
                generate_html(combined, runs=all_runs, output_path=combined_path)
                print(f"Combined report: {combined_path}", flush=True)
        except ImportError:
            print("\nWARNING: jinja2 not installed, skipping HTML report.", flush=True)
            print("Install with: pip3 install jinja2", flush=True)
        except Exception as e:
            print(f"\nWARNING: HTML report generation failed: {e}", flush=True)

    print(f"\nTotal wall time: {wall_elapsed / 60:.1f} min", flush=True)
    print(f"Results: {results_path}", flush=True)


if __name__ == "__main__":
    main()
