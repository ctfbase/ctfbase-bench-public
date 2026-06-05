#!/usr/bin/env python3
"""
Prepare workdirs for benchmark runs.

For each (model, task, trial) combination, creates:
  /tmp/ctfbase-bench/{run_id}/{model}/{task_id}/trial_{n}/
    ├── opencode.json           # {} or MCP config (--ctfbase)
    ├── .opencode/agents/*.md   # (if --agents-dir provided)
    ├── config/network-whitelist.txt  # for iptables
    └── (challenge files)       # from tasks/{category}/{task_id}/challenge/

Usage:
  python runners/prepare.py --run-id 20260503_pilot --models anthropic/claude-opus-4-5 --trials 3
  python runners/prepare.py --run-id test --models anthropic/claude-opus-4-5 --agents-dir agents/custom --ctfbase
"""

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import yaml

BENCH_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BENCH_DIR / "config"
TASKS_DIR = BENCH_DIR / "tasks"
WORKDIR_BASE = Path("/tmp/ctfbase-bench")

# opencode.json for CTFBase MCP integration
CTFBASE_CONFIG = {
    "mcp": {
        "ctfbase": {
            "type": "remote",
            "url": "https://mcp.ctfbase.com/mcp",
            "headers": {
                "Authorization": "Bearer {env:CTFBASE_API_KEY}"
            }
        }
    }
}


def load_config() -> dict:
    """Load benchmark config.yaml."""
    config_path = BENCH_DIR / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


def load_task_list() -> dict[str, dict]:
    """Load task_list.yaml, return dict keyed by task id."""
    with open(BENCH_DIR / "task_list.yaml") as f:
        data = yaml.safe_load(f)
    return {t["id"]: t for t in data["tasks"]}


def get_task_dir(task_info: dict) -> Path:
    """Get the task directory from task_info path field or fallback."""
    if "path" in task_info:
        return BENCH_DIR / task_info["path"]
    # Fallback: tasks/{category}/{task_id}/
    return TASKS_DIR / task_info.get("category", "misc") / task_info["id"]


def load_task_metadata(task_id: str, task_info: dict) -> dict:
    """Load metadata.yaml for a specific task."""
    task_dir = get_task_dir(task_info)
    meta_path = task_dir / "metadata.yaml"
    if not meta_path.exists():
        print(f"  WARNING: metadata.yaml not found for {task_id}, using task_list info")
        return task_info
    with open(meta_path) as f:
        return yaml.safe_load(f)


def strip_model_from_frontmatter(content: str) -> str:
    """Remove `model:` line from YAML frontmatter."""
    lines = content.split("\n")
    result = []
    in_frontmatter = False
    fm_count = 0

    for line in lines:
        if line.strip() == "---":
            fm_count += 1
            if fm_count == 1:
                in_frontmatter = True
            elif fm_count == 2:
                in_frontmatter = False
            result.append(line)
            continue

        if in_frontmatter and line.strip().startswith("model:"):
            continue

        result.append(line)

    return "\n".join(result)


def compute_input_hash(
    prompt: str,
    challenge_dir: Path | None,
    agents_dir: Path | None,
    opencode_config: dict,
) -> str:
    """Compute sha256 hash of all inputs for reproducibility."""
    h = hashlib.sha256()
    h.update(prompt.encode("utf-8"))

    # Challenge files (sorted for determinism)
    if challenge_dir and challenge_dir.exists():
        for f in sorted(challenge_dir.rglob("*")):
            if f.is_file():
                h.update(f.name.encode("utf-8"))
                h.update(f.read_bytes())

    # Agent files (sorted)
    if agents_dir and agents_dir.exists():
        for f in sorted(agents_dir.glob("*.md")):
            h.update(f.name.encode("utf-8"))
            h.update(f.read_bytes())

    # Config
    h.update(json.dumps(opencode_config, sort_keys=True).encode("utf-8"))

    return f"sha256:{h.hexdigest()}"


def prepare_workdir(
    run_id: str,
    model: str,
    mode: str,
    task_id: str,
    trial: int,
    task_info: dict,
    config: dict,
    agents_dir: Path | None = None,
    ctfbase: bool = False,
) -> dict:
    """Prepare one workdir. Returns metadata dict."""
    model_safe = model.replace("/", "_")

    workdir = WORKDIR_BASE / run_id / model_safe / mode / task_id / f"trial_{trial}"
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    # 1. opencode.json
    oc_config = CTFBASE_CONFIG if ctfbase else {}

    (workdir / "opencode.json").write_text(
        json.dumps(oc_config, indent=2, ensure_ascii=False) + "\n"
    )

    # 2. Agent files (if agents_dir provided and mode is not bare)
    agents_dest = None
    if agents_dir and agents_dir.exists():
        agents_dest = workdir / ".opencode" / "agents"
        agents_dest.mkdir(parents=True)

        for src in sorted(agents_dir.glob("*.md")):
            content = src.read_text(encoding="utf-8")
            # Strip model: from frontmatter (subagent inherits primary model)
            if config.get("agent_overrides", {}).get("strip_model_frontmatter", True):
                content = strip_model_from_frontmatter(content)
            (agents_dest / src.name).write_text(content, encoding="utf-8")

    # 3. Challenge files
    task_dir = get_task_dir(task_info)
    challenge_src = task_dir / "challenge"
    challenge_dest = None
    if challenge_src.exists():
        for item in challenge_src.iterdir():
            dest = workdir / item.name
            if item.is_dir():
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        challenge_dest = challenge_src

    # 4. Copy network whitelist config
    whitelist_src = CONFIG_DIR / "network-whitelist.txt"
    if whitelist_src.exists():
        config_dest = workdir / "config"
        config_dest.mkdir(exist_ok=True)
        shutil.copy2(whitelist_src, config_dest / "network-whitelist.txt")

    # 5. Load prompt and timeout from metadata
    metadata = load_task_metadata(task_id, task_info)
    prompt_path = task_dir / "prompt.md"
    if prompt_path.exists():
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    else:
        prompt = metadata.get("prompt", "Solve the CTF challenge in the current directory. Find the flag.")

    timeout_seconds = metadata.get("timeout_seconds", None)

    # 6. Compute input hash
    input_hash = compute_input_hash(prompt, challenge_dest, agents_dest, oc_config)

    return {
        "workdir": str(workdir),
        "run_id": run_id,
        "model": model,
        "mode": mode,
        "task_id": task_id,
        "task_path": task_info.get("path", f"tasks/cybench/{task_id}"),
        "trial_index": trial,
        "category": task_info.get("category", "misc"),
        "difficulty": task_info.get("difficulty", "medium"),
        "task_type": task_info.get("type", "offline"),
        "timeout_seconds": timeout_seconds,
        "prompt": prompt,
        "input_hash": input_hash,
    }


def prepare(
    run_id: str,
    models: list[str],
    modes: list[str] | None = None,
    tasks: list[str] | None = None,
    trials: int = 3,
    output: str | None = None,
    agents_dir: str | Path | None = None,
    ctfbase: bool = False,
) -> Path:
    """Prepare benchmark workdirs. Returns path to manifest.json.

    Args:
        run_id: Unique run identifier.
        models: List of OpenRouter model IDs.
        modes: List of modes. With --agents-dir: ["bare", "custom"].
               Without: ["bare"].
        tasks: List of task IDs (default: all from task_list.yaml).
        trials: Number of trials per cell.
        output: Custom manifest output path.
        agents_dir: Path to directory with agent .md files.
        ctfbase: Whether to inject CTFBase MCP config.

    Returns:
        Path to the generated manifest.json.
    """
    if modes is None:
        modes = ["bare"]

    config = load_config()
    task_list = load_task_list()

    if tasks is None:
        task_ids = list(task_list.keys())
    else:
        task_ids = tasks

    # Resolve agents dir
    resolved_agents_dir = None
    if agents_dir:
        resolved_agents_dir = Path(agents_dir)
        if not resolved_agents_dir.is_absolute():
            resolved_agents_dir = BENCH_DIR / resolved_agents_dir
        if not resolved_agents_dir.exists():
            raise FileNotFoundError(f"Agents directory not found: {resolved_agents_dir}")
        agent_files = list(resolved_agents_dir.glob("*.md"))
        if not agent_files:
            raise FileNotFoundError(f"No .md agent files found in: {resolved_agents_dir}")

    # Validate modes
    valid_modes = {"bare", "custom"}
    for mode in modes:
        if mode not in valid_modes:
            raise ValueError(f"Unknown mode '{mode}'. Valid modes: {', '.join(valid_modes)}")

    if "custom" in modes and not resolved_agents_dir:
        raise ValueError("Mode 'custom' requires --agents-dir")

    # Validate tasks
    for task_id in task_ids:
        if task_id not in task_list:
            raise ValueError(f"Unknown task '{task_id}'")

    # Generate all workdirs
    manifest = []
    total = len(models) * len(modes) * len(task_ids) * trials
    count = 0

    print(f"Preparing {total} workdirs for run '{run_id}'...")
    print(f"  Models: {models}")
    print(f"  Modes: {modes}")
    if resolved_agents_dir:
        print(f"  Agents: {resolved_agents_dir}")
    if ctfbase:
        print(f"  CTFBase: enabled")
    print(f"  Tasks: {task_ids}")
    print(f"  Trials: {trials}")
    print()

    for model in models:
        for mode in modes:
            for task_id in task_ids:
                task_info = task_list[task_id]
                for trial in range(trials):
                    count += 1
                    entry = prepare_workdir(
                        run_id, model, mode, task_id, trial, task_info, config,
                        agents_dir=resolved_agents_dir if mode == "custom" else None,
                        ctfbase=ctfbase,
                    )
                    manifest.append(entry)
                    print(f"  [{count}/{total}] {model} / {mode} / {task_id} / trial_{trial}")

    # Write manifest
    if output:
        out_path = Path(output)
    else:
        out_path = WORKDIR_BASE / run_id / "manifest.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {count} workdirs prepared.")
    print(f"Manifest: {out_path}")

    return out_path


def main():
    parser = argparse.ArgumentParser(description="Prepare benchmark workdirs")
    parser.add_argument("--run-id", required=True, help="Run identifier")
    parser.add_argument("--models", required=True, help="Comma-separated model IDs")
    parser.add_argument("--modes", default=None, help="Comma-separated modes: bare, custom (default: auto)")
    parser.add_argument("--tasks", default=None, help="Comma-separated task IDs (default: all)")
    parser.add_argument("--trials", type=int, default=3, help="Number of trials per cell")
    parser.add_argument("--agents-dir", default=None, help="Path to agent .md files")
    parser.add_argument("--ctfbase", action="store_true", help="Enable CTFBase MCP knowledge base")
    parser.add_argument("--output", default=None, help="Output manifest JSON path")
    args = parser.parse_args()

    models = [m.strip() for m in args.models.split(",")]
    task_ids = [t.strip() for t in args.tasks.split(",")] if args.tasks else None

    # Auto-determine modes
    if args.modes:
        modes = [m.strip() for m in args.modes.split(",")]
    elif args.agents_dir:
        modes = ["bare", "custom"]
    else:
        modes = ["bare"]

    try:
        prepare(
            run_id=args.run_id,
            models=models,
            modes=modes,
            tasks=task_ids,
            trials=args.trials,
            output=args.output,
            agents_dir=args.agents_dir,
            ctfbase=args.ctfbase,
        )
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
