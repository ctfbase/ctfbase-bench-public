#!/usr/bin/env python3
"""
Main benchmark runner.

Iterates over (model, mode, task, trial) combinations from a manifest,
launches each run in a Docker container, captures NDJSON output,
monitors cost/time budgets, and invokes grading.

Usage:
  python runners/runner.py --manifest /tmp/ctfbase-bench/20260503_pilot/manifest.json
  python runners/runner.py --manifest manifest.json --parallel 4 --dry-run
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

BENCH_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BENCH_DIR / "grading"))

from grade import grade_session  # noqa: E402
from scoring import compute_task_score  # noqa: E402

# ---------------------------------------------------------------------------
# Graceful shutdown machinery
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()
_active_processes: list[subprocess.Popen] = []
_active_processes_lock = threading.Lock()


def _kill_active_processes():
    """Kill all tracked child processes."""
    with _active_processes_lock:
        for p in _active_processes:
            try:
                p.kill()
            except OSError:
                pass


def install_shutdown_handler():
    """Install SIGINT/SIGTERM handler for graceful shutdown.

    First Ctrl+C: set shutdown flag + kill all running containers.
    Second Ctrl+C: raise KeyboardInterrupt for immediate exit.
    """
    def handler(signum, frame):
        if _shutdown_event.is_set():
            # Second signal → force exit
            print("\nForce shutdown.", flush=True)
            _kill_active_processes()
            raise KeyboardInterrupt
        _shutdown_event.set()
        print("\nShutdown requested — killing running containers...", flush=True)
        print("  Press Ctrl+C again to force-quit.", flush=True)
        _kill_active_processes()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def load_config() -> dict:
    with open(BENCH_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def get_timeout(entry: dict, config: dict) -> int:
    """Get timeout in seconds. Priority: entry.timeout_seconds > config per difficulty > 1800."""
    if "timeout_seconds" in entry and entry["timeout_seconds"]:
        return entry["timeout_seconds"]
    timeouts = config.get("timeouts", {})
    return timeouts.get(entry.get("difficulty", "medium"), 1800)


def build_docker_run_cmd(entry: dict, config: dict) -> list[str]:
    """Build docker run command for an offline task."""
    timeout_sec = get_timeout(entry, config)
    docker_config = config.get("docker", {})
    image = docker_config.get("image", "ctfbase-bench")
    stop_timeout = docker_config.get("stop_timeout", 10)

    cmd = [
        "docker", "run", "--rm",
        "-e", "OPENROUTER_API_KEY",
        "-e", "CTFBASE_API_KEY",
        "-e", "NETWORK_WHITELIST=1",
        "--cap-add=NET_ADMIN",
        f"--stop-timeout={stop_timeout}",
        "-v", f"{entry['workdir']}:/workspace",
        image,
        "timeout", str(timeout_sec),
        "opencode", "run",
        "--dangerously-skip-permissions",
        "--pure",
        "--model", f"openrouter/{entry['model']}",
        "--format", "json",
        entry["prompt"],
    ]

    return cmd


def build_compose_cmd(entry: dict, config: dict) -> tuple[list[str], list[str], dict, str]:
    """Build docker compose command for a docker task.

    Returns (build_cmd, run_cmd, env_vars, project_name).
    project_name is unique per task+trial to avoid conflicts in parallel mode.
    """
    timeout_sec = get_timeout(entry, config)
    # Resolve task dir from entry
    if "task_path" in entry:
        task_dir = BENCH_DIR / entry["task_path"]
    else:
        task_dir = BENCH_DIR / "tasks" / "cybench" / entry["task_id"]
    # Look for bench-compose.yml first, then docker-compose.yml, then docker/docker-compose.yml
    compose_file = None
    for candidate in [
        task_dir / "bench-compose.yml",
        task_dir / "docker-compose.yml",
        task_dir / "docker" / "docker-compose.yml",
    ]:
        if candidate.exists():
            compose_file = candidate
            break
    if compose_file is None:
        raise FileNotFoundError(f"No compose file found for {entry['task_id']} in {task_dir}")

    # Unique project name to avoid conflicts when running trials in parallel
    project_name = f"bench-{entry['task_id']}-t{entry['trial_index']}"

    env = {
        "WORKDIR": entry["workdir"],
        "MODEL": entry["model"],
        "TIMEOUT": str(timeout_sec),
        "PROMPT": entry["prompt"],
        "OPENROUTER_API_KEY": os.environ.get("OPENROUTER_API_KEY", ""),
        "CTFBASE_API_KEY": os.environ.get("CTFBASE_API_KEY", ""),
    }

    # Build target image first (separate step, output to devnull)
    build_cmd = [
        "docker", "compose",
        "-f", str(compose_file),
        "-p", project_name,
        "build", "--quiet",
    ]

    run_cmd = [
        "docker", "compose",
        "-f", str(compose_file),
        "-p", project_name,
        "up",
        "--no-build",
        "--exit-code-from", "agent",
        "--no-log-prefix",
        "--attach", "agent",
    ]

    return build_cmd, run_cmd, env, project_name


def run_single(entry: dict, config: dict, logs_dir: Path, dry_run: bool = False) -> dict:
    """Run a single benchmark task. Returns result dict."""
    model_safe = entry["model"].replace("/", "_")
    task_log_dir = logs_dir / model_safe / entry["mode"] / entry["task_id"] / f"trial_{entry['trial_index']}"
    task_log_dir.mkdir(parents=True, exist_ok=True)

    session_path = task_log_dir / "session.jsonl"
    result_path = task_log_dir / "result.json"

    if dry_run:
        print(f"  [DRY RUN] {entry['model']} / {entry['mode']} / {entry['task_id']} / trial_{entry['trial_index']}", flush=True)
        return {"status": "dry_run", **entry}

    print(f"  RUNNING: {entry['model']} / {entry['mode']} / {entry['task_id']} / trial_{entry['trial_index']}", flush=True)

    # Build command
    is_docker_task = entry.get("task_type") == "docker"
    extra_env = None
    build_cmd = None
    compose_project = None

    if is_docker_task:
        build_cmd, cmd, extra_env, compose_project = build_compose_cmd(entry, config)
    else:
        cmd = build_docker_run_cmd(entry, config)

    # Start process
    start_time = time.time()
    env = {**os.environ}
    if extra_env:
        env.update(extra_env)

    # Build target image (docker tasks only)
    if build_cmd:
        build_result = subprocess.run(
            build_cmd, env=env, capture_output=True, timeout=300,
        )
        if build_result.returncode != 0:
            elapsed = time.time() - start_time
            print(f"  BUILD FAILED: {entry['model']} / {entry['task_id']} / trial_{entry['trial_index']} (exit {build_result.returncode})", flush=True)
            if not session_path.exists():
                session_path.write_text("")
            return {
                "run_id": entry["run_id"], "task_id": entry["task_id"],
                "model": f"openrouter/{entry['model']}", "mode": entry["mode"],
                "trial_index": entry["trial_index"], "category": entry["category"],
                "difficulty": entry["difficulty"], "flag_found": False,
                "flag_submitted": None, "flag_correct": False,
                "total_cost_usd": 0, "total_tokens_input": 0,
                "total_tokens_output": 0, "total_tokens_cache_read": 0,
                "time_seconds": round(elapsed, 1),
                "timeout_seconds": get_timeout(entry, config),
                "steps": 0,
                "exit_reason": f"build_failed_{build_result.returncode}",
                "input_hash": entry["input_hash"],
            }

    timeout_sec = get_timeout(entry, config)
    watchdog_grace = config.get("docker", {}).get("watchdog_grace", 30)
    max_cost = config.get("limits", {}).get("max_cost_usd_per_task", 5.0)

    process = None
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # avoid deadlock; stderr not needed for grading
            env=env,
        )

        # Track process for shutdown handler
        with _active_processes_lock:
            _active_processes.append(process)

        # Write stdout to session.jsonl in real-time + inline cost cap
        cumulative_cost = 0.0
        with open(session_path, "w") as session_file:
            for line in process.stdout:
                decoded = line.decode("utf-8", errors="replace")
                session_file.write(decoded)
                session_file.flush()

                # Inline cost check (real schema: part.cost)
                try:
                    event = json.loads(decoded.strip())
                    if event.get("type") == "step_finish":
                        part = event.get("part", {})
                        cost = part.get("cost", 0) or 0
                        cumulative_cost += cost
                        if cumulative_cost > max_cost:
                            print(f"    BUDGET CAP: ${cumulative_cost:.2f} > ${max_cost:.2f}, killing", flush=True)
                            process.kill()
                            break
                except (json.JSONDecodeError, KeyError):
                    pass

        # Watchdog: if container doesn't exit within timeout + grace, force-kill
        try:
            process.wait(timeout=watchdog_grace)
        except subprocess.TimeoutExpired:
            print(f"    WATCHDOG: force-killing after {watchdog_grace}s grace", flush=True)
            process.kill()
            process.wait(timeout=10)

        elapsed = time.time() - start_time
        exit_code = process.returncode

        # Determine exit reason
        if _shutdown_event.is_set():
            exit_reason = "interrupted"
        elif exit_code == 124:
            exit_reason = "timeout"
        elif exit_code == 137:
            exit_reason = "killed"
        elif exit_code == 0:
            exit_reason = "completed"
        else:
            exit_reason = f"error_{exit_code}"

    except KeyboardInterrupt:
        elapsed = time.time() - start_time
        exit_reason = "interrupted"
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if not session_path.exists():
            session_path.write_text("")

    except Exception as e:
        elapsed = time.time() - start_time
        exit_reason = f"exception: {e}"
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if not session_path.exists():
            session_path.write_text("")

    finally:
        # Untrack process
        if process is not None:
            with _active_processes_lock:
                try:
                    _active_processes.remove(process)
                except ValueError:
                    pass

    # Docker compose cleanup (always, even after interrupt)
    if is_docker_task:
        if "task_path" in entry:
            cleanup_dir = BENCH_DIR / entry["task_path"]
        else:
            cleanup_dir = BENCH_DIR / "tasks" / "cybench" / entry["task_id"]
        # Use same project name to clean up the right containers
        cleanup_project = compose_project or f"bench-{entry['task_id']}-t{entry['trial_index']}"
        for candidate in [
            cleanup_dir / "bench-compose.yml",
            cleanup_dir / "docker-compose.yml",
            cleanup_dir / "docker" / "docker-compose.yml",
        ]:
            if candidate.exists():
                try:
                    subprocess.run(
                        ["docker", "compose", "-f", str(candidate),
                         "-p", cleanup_project, "down", "-v"],
                        capture_output=True,
                        timeout=30,
                    )
                except Exception:
                    pass
                break

    # Grade + save result (wrapped in try/except to never lose results)
    task_label = f"{entry['model']} / {entry['task_id']} / trial_{entry['trial_index']}"
    try:
        # Resolve task dir from entry path or fallback
        if "task_path" in entry:
            task_dir = BENCH_DIR / entry["task_path"]
        else:
            task_dir = BENCH_DIR / "tasks" / "cybench" / entry["task_id"]
        metadata_path = task_dir / "metadata.yaml"
        grade_result = grade_session(session_path, metadata_path)

        # Classify provider_error: steps=0, cost=0, error_* exit
        actual_exit_reason = exit_reason
        if (
            grade_result.get("steps", 0) == 0
            and grade_result.get("total_cost_usd", 0) == 0
            and exit_reason.startswith("error_")
        ):
            actual_exit_reason = "provider_error"

        # Build result
        result = {
            "run_id": entry["run_id"],
            "task_id": entry["task_id"],
            "model": f"openrouter/{entry['model']}",
            "mode": entry["mode"],
            "trial_index": entry["trial_index"],
            "category": entry["category"],
            "difficulty": entry["difficulty"],
            "flag_found": grade_result.get("flag_found", False),
            "flag_submitted": grade_result.get("flag_submitted"),
            "flag_correct": grade_result.get("flag_correct", False),
            "total_cost_usd": grade_result.get("total_cost_usd", 0),
            "total_tokens_input": grade_result.get("total_tokens_input", 0),
            "total_tokens_output": grade_result.get("total_tokens_output", 0),
            "total_tokens_cache_read": grade_result.get("total_tokens_cache_read", 0),
            "time_seconds": round(elapsed, 1),
            "timeout_seconds": timeout_sec,
            "steps": grade_result.get("steps", 0),
            "exit_reason": actual_exit_reason,
            "input_hash": entry["input_hash"],
        }

    except Exception as e:
        # Grading failed — still save a result so it's never lost
        print(f"  GRADE ERROR: {task_label}: {e}", flush=True)
        result = {
            "run_id": entry["run_id"],
            "task_id": entry["task_id"],
            "model": f"openrouter/{entry['model']}",
            "mode": entry["mode"],
            "trial_index": entry["trial_index"],
            "category": entry.get("category", ""),
            "difficulty": entry.get("difficulty", ""),
            "flag_found": False, "flag_submitted": None, "flag_correct": False,
            "total_cost_usd": 0, "total_tokens_input": 0,
            "total_tokens_output": 0, "total_tokens_cache_read": 0,
            "time_seconds": round(elapsed, 1),
            "timeout_seconds": timeout_sec,
            "steps": 0,
            "exit_reason": f"grade_error: {e}",
            "input_hash": entry.get("input_hash", ""),
        }

    # Compute task score
    score_info = compute_task_score(result, max_cost_usd=max_cost)
    result["task_score"] = score_info["task_score"]
    result["max_task_score"] = score_info["max_task_score"]

    # Save result (always)
    with open(result_path, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    status = "SOLVED" if result["flag_correct"] else "FAILED"
    score_str = f", score={result['task_score']:.0f}" if result["task_score"] > 0 else ""
    print(f"  {status}: {task_label} ({elapsed:.0f}s, ${result['total_cost_usd']:.2f}{score_str})", flush=True)

    return result


def run(
    manifest_path: str | Path,
    parallel: int = 1,
    dry_run: bool = False,
    logs_dir: str | Path | None = None,
) -> Path:
    """Run benchmark from a manifest file. Returns path to results JSON.

    Args:
        manifest_path: Path to manifest.json from prepare step.
        parallel: Number of parallel Docker containers.
        dry_run: Print commands without running.
        logs_dir: Override logs directory.

    Returns:
        Path to the generated results JSON file.
    """
    config = load_config()

    # Load manifest
    with open(manifest_path) as f:
        manifest = json.load(f)

    if not manifest:
        print("Empty manifest, nothing to run.")
        return Path("/dev/null")

    run_id = manifest[0]["run_id"]
    effective_logs_dir = Path(logs_dir) if logs_dir else BENCH_DIR / "logs" / run_id
    effective_logs_dir.mkdir(parents=True, exist_ok=True)

    effective_parallel = parallel or config.get("runner", {}).get("parallel_containers", 4)

    print(f"CTFBase-Bench Runner", flush=True)
    print(f"  Run ID: {run_id}", flush=True)
    print(f"  Tasks: {len(manifest)}", flush=True)
    print(f"  Parallel: {effective_parallel}", flush=True)
    print(f"  Logs: {effective_logs_dir}", flush=True)
    print(flush=True)

    # Verify API keys
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise EnvironmentError("OPENROUTER_API_KEY not set")

    # Check CTFBASE_API_KEY if any workdir has CTFBase MCP config
    has_ctfbase = False
    for entry in manifest:
        oc_path = Path(entry["workdir"]) / "opencode.json"
        if oc_path.exists():
            try:
                oc = json.loads(oc_path.read_text())
                if oc.get("mcp", {}).get("ctfbase"):
                    has_ctfbase = True
                    break
            except (json.JSONDecodeError, OSError):
                pass
    if has_ctfbase and not os.environ.get("CTFBASE_API_KEY"):
        raise EnvironmentError("CTFBASE_API_KEY not set (required when --ctfbase is enabled)")

    # Verify Docker image exists (unless dry run)
    if not dry_run:
        image = config.get("docker", {}).get("image", "ctfbase-bench")
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Docker image '{image}' not found. Build it first: docker build -t ctfbase-bench ."
            )

    # Run all tasks
    all_results = []
    summary_path = BENCH_DIR / "results" / f"{run_id}_results.json"
    interrupted = False

    try:
        if effective_parallel <= 1:
            for i, entry in enumerate(manifest):
                if _shutdown_event.is_set():
                    remaining = len(manifest) - i
                    print(f"\n  Shutdown: skipping {remaining} remaining tasks.", flush=True)
                    interrupted = True
                    break
                res = run_single(entry, config, effective_logs_dir, dry_run)
                all_results.append(res)
        else:
            with ThreadPoolExecutor(max_workers=effective_parallel) as executor:
                # Submit tasks lazily to allow cancellation
                pending = list(manifest)
                futures: dict = {}

                # Submit initial batch
                for _ in range(min(effective_parallel, len(pending))):
                    entry = pending.pop(0)
                    futures[executor.submit(run_single, entry, config, effective_logs_dir, dry_run)] = entry

                while futures:
                    # Wait for any future to complete
                    done = set()
                    for future in as_completed(futures):
                        done.add(future)
                        try:
                            res = future.result()
                            all_results.append(res)
                        except Exception as e:
                            entry = futures[future]
                            print(f"  ERROR in {entry['task_id']}: {e}", flush=True)
                            all_results.append({"status": "error", "error": str(e), **entry})

                        # Submit next task if available and not shutting down
                        if pending and not _shutdown_event.is_set():
                            next_entry = pending.pop(0)
                            futures[executor.submit(run_single, next_entry, config, effective_logs_dir, dry_run)] = next_entry
                        break  # process one at a time to check shutdown

                    # Remove completed futures
                    for f in done:
                        del futures[f]

                    if _shutdown_event.is_set() and not futures:
                        break
                    if _shutdown_event.is_set():
                        remaining = len(pending) + len(futures)
                        print(f"\n  Shutdown: waiting for {len(futures)} running tasks, skipping {len(pending)} pending.", flush=True)
                        interrupted = True
                        pending.clear()
                        # Wait for running futures to finish (they've been killed by signal handler)
                        for future in as_completed(futures):
                            try:
                                res = future.result()
                                all_results.append(res)
                            except Exception:
                                pass
                        break

    except KeyboardInterrupt:
        interrupted = True
        print("\nForce interrupted.", flush=True)

    finally:
        # Always save results (even partial)
        _save_results(all_results, summary_path, interrupted)

    return summary_path


def _save_results(all_results: list[dict], summary_path: Path, interrupted: bool = False):
    """Save results JSON and print summary."""
    solved = sum(1 for r in all_results if r.get("flag_correct"))
    total = len(all_results)
    total_cost = sum(r.get("total_cost_usd", 0) for r in all_results)

    status = "Interrupted" if interrupted else "Run complete"
    print(f"\n{'=' * 60}", flush=True)
    print(f"{status}: {solved}/{total} solved, total cost: ${total_cost:.2f}", flush=True)

    if all_results:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        with open(summary_path, "w") as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        print(f"Results: {summary_path}", flush=True)
    else:
        print("No results to save.", flush=True)


def main():
    parser = argparse.ArgumentParser(description="CTFBase-Bench runner")
    parser.add_argument("--manifest", required=True, help="Path to manifest.json from prepare.py")
    parser.add_argument("--parallel", type=int, default=1, help="Number of parallel containers")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running")
    parser.add_argument("--logs-dir", default=None, help="Override logs directory")
    args = parser.parse_args()

    try:
        run(
            manifest_path=args.manifest,
            parallel=args.parallel,
            dry_run=args.dry_run,
            logs_dir=args.logs_dir,
        )
    except (EnvironmentError, RuntimeError) as e:
        print(f"ERROR: {e}", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
