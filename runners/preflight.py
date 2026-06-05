#!/usr/bin/env python3
"""
Pre-flight checks before benchmark run.

1. Validate API key + fetch model registry from OpenRouter
2. Verify each requested model exists
3. Estimate costs based on model pricing × typical token usage
4. Show summary and ask user to confirm

Usage (from run.py):
    from preflight import preflight
    preflight(manifest, config, skip_confirm=False)
"""

import json
import os
import sys
import urllib.request
import urllib.error

# Typical token usage per task difficulty (input + output per run).
# Conservative estimates based on CTF benchmark observations:
#   easy  — ~2-3 steps, short context
#   medium — ~5-8 steps, moderate context
#   hard  — ~10-15 steps, long context with tool output
TYPICAL_TOKENS = {
    "easy":   {"input": 20_000, "output": 3_000},
    "medium": {"input": 50_000, "output": 8_000},
    "hard":   {"input": 100_000, "output": 15_000},
}

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
REQUEST_TIMEOUT = 15  # seconds


def fetch_openrouter_models(api_key: str | None = None) -> dict[str, dict] | None:
    """Fetch model registry from OpenRouter API.

    The /api/v1/models endpoint is public (no auth required for listing).
    If api_key is provided, it's sent for authentication validation.

    Returns dict {model_id: model_info} or None if API unreachable.
    Raises SystemExit on 401 (invalid key).
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    req = urllib.request.Request(OPENROUTER_MODELS_URL, headers=headers)

    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("ERROR: Invalid OPENROUTER_API_KEY (401 Unauthorized).", flush=True)
            sys.exit(1)
        print(f"  WARNING: OpenRouter API returned HTTP {e.code}, skipping model validation.", flush=True)
        return None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  WARNING: Cannot reach OpenRouter API ({e}), skipping model validation.", flush=True)
        return None

    return {m["id"]: m for m in data.get("data", [])}


def validate_models(
    models: list[str],
    registry: dict[str, dict],
) -> list[tuple[str, dict | None]]:
    """Check each model exists in registry. Returns list of (model_id, info_or_None)."""
    results = []
    for model in models:
        info = registry.get(model)
        results.append((model, info))
    return results


def _parse_pricing(model_info: dict) -> tuple[float, float]:
    """Extract per-token pricing (input, output) from model info.

    OpenRouter returns pricing as string cost per token.
    Returns (cost_per_input_token, cost_per_output_token).
    """
    pricing = model_info.get("pricing", {})
    try:
        prompt_price = float(pricing.get("prompt", "0"))
        completion_price = float(pricing.get("completion", "0"))
    except (ValueError, TypeError):
        prompt_price = 0.0
        completion_price = 0.0
    return prompt_price, completion_price


def _format_price_per_million(price_per_token: float) -> str:
    """Format per-token price as $/M tokens."""
    per_million = price_per_token * 1_000_000
    if per_million == 0:
        return "FREE"
    if per_million < 0.01:
        return f"${per_million:.4f}/M"
    if per_million < 1:
        return f"${per_million:.3f}/M"
    return f"${per_million:.2f}/M"


def estimate_cost(
    manifest: list[dict],
    registry: dict[str, dict],
    max_cost_per_task: float,
) -> dict:
    """Estimate total run cost.

    Returns {
        "per_model": {model_id: {"runs": int, "estimate": float}},
        "total_estimate": float,
        "total_worst_case": float,
    }
    """
    per_model: dict[str, dict] = {}

    for entry in manifest:
        model = entry["model"]
        difficulty = entry.get("difficulty", "medium")
        tokens = TYPICAL_TOKENS.get(difficulty, TYPICAL_TOKENS["medium"])

        if model not in per_model:
            info = registry.get(model, {})
            in_price, out_price = _parse_pricing(info)
            per_model[model] = {
                "runs": 0,
                "estimate": 0.0,
                "input_price": in_price,
                "output_price": out_price,
            }

        entry_cost = (
            tokens["input"] * per_model[model]["input_price"]
            + tokens["output"] * per_model[model]["output_price"]
        )
        per_model[model]["runs"] += 1
        per_model[model]["estimate"] += entry_cost

    total_estimate = sum(m["estimate"] for m in per_model.values())
    total_worst_case = len(manifest) * max_cost_per_task

    return {
        "per_model": per_model,
        "total_estimate": total_estimate,
        "total_worst_case": total_worst_case,
    }


def _print_summary(
    model_results: list[tuple[str, dict | None]],
    cost_info: dict | None,
    manifest: list[dict],
    max_cost_per_task: float,
) -> None:
    """Print pre-flight summary to stdout."""
    # Count dimensions
    models = sorted({e["model"] for e in manifest})
    modes = sorted({e["mode"] for e in manifest})
    tasks = sorted({e["task_id"] for e in manifest})
    trials = max((e["trial_index"] for e in manifest), default=0) + 1

    print(flush=True)
    print("Pre-flight Check", flush=True)
    print("=" * 56, flush=True)

    # Models
    print("Models:", flush=True)
    for model_id, info in model_results:
        if info is None:
            print(f"  ✗ {model_id:<35s} NOT FOUND", flush=True)
        else:
            in_price, out_price = _parse_pricing(info)
            in_fmt = _format_price_per_million(in_price)
            out_fmt = _format_price_per_million(out_price)
            print(f"  ✓ {model_id:<35s} {in_fmt} in · {out_fmt} out", flush=True)

    print(flush=True)
    print(f"Plan: {len(models)} model{'s' if len(models) != 1 else ''}"
          f" × {len(modes)} mode{'s' if len(modes) != 1 else ''}"
          f" × {len(tasks)} task{'s' if len(tasks) != 1 else ''}"
          f" × {trials} trial{'s' if trials != 1 else ''}"
          f" = {len(manifest)} runs", flush=True)

    # Timing estimate
    easy = sum(1 for e in manifest if e.get("difficulty") == "easy")
    medium = sum(1 for e in manifest if e.get("difficulty") == "medium")
    hard = sum(1 for e in manifest if e.get("difficulty") == "hard")
    if easy or medium or hard:
        print(f"  Tasks by difficulty: {easy} easy, {medium} medium, {hard} hard", flush=True)

    # Cost
    if cost_info:
        print(flush=True)
        print("Cost Estimate (typical usage):", flush=True)
        for model_id in models:
            m = cost_info["per_model"].get(model_id, {})
            runs = m.get("runs", 0)
            est = m.get("estimate", 0)
            print(f"  {model_id:<35s} {runs:>4d} runs  ~${est:>8.2f}", flush=True)
        print(f"  {'─' * 52}", flush=True)
        print(f"  {'Total estimate:':<40s}  ~${cost_info['total_estimate']:>8.2f}", flush=True)
        print(f"  {'Worst case (all hit cap):':<40s}   ${cost_info['total_worst_case']:>8.2f}", flush=True)
    else:
        # No registry — show worst case only
        worst = len(manifest) * max_cost_per_task
        print(flush=True)
        print(f"Cost: worst case (all hit ${max_cost_per_task:.2f} cap) = ${worst:.2f}", flush=True)

    print(flush=True)


def preflight(
    manifest: list[dict],
    config: dict,
    skip_confirm: bool = False,
) -> None:
    """Run pre-flight checks. Aborts (sys.exit) on failure.

    Args:
        manifest: List of manifest entries from prepare().
        config: Loaded config.yaml dict.
        skip_confirm: If True, skip interactive confirmation (--yes or --dry-run).
    """
    if not manifest:
        return

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    max_cost = config.get("limits", {}).get("max_cost_usd_per_task", 5.0)

    # Unique models in manifest
    unique_models = sorted({e["model"] for e in manifest})

    # 1. Fetch model registry (public endpoint, works without API key)
    print("Checking models on OpenRouter...", flush=True)
    registry = fetch_openrouter_models(api_key or None)

    # 2. Validate models
    model_results: list[tuple[str, dict | None]]
    cost_info = None
    missing_models = []

    if registry is not None:
        model_results = validate_models(unique_models, registry)
        missing_models = [m for m, info in model_results if info is None]

        # 3. Estimate costs
        cost_info = estimate_cost(manifest, registry, max_cost)
    else:
        # Can't validate — show as unknown
        model_results = [(m, None) for m in unique_models]

    # 4. Print summary
    _print_summary(model_results, cost_info, manifest, max_cost)

    # 5. Abort if models not found (only when registry was fetched)
    if registry is not None and missing_models:
        for m in missing_models:
            print(f"ERROR: Model '{m}' not found on OpenRouter.", flush=True)
        print(flush=True)
        print("Fix model names and try again.", flush=True)
        sys.exit(1)

    # 6. Ask confirmation
    if not skip_confirm:
        try:
            answer = input("Continue? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.", flush=True)
            sys.exit(1)

        if answer not in ("y", "yes"):
            print("Aborted.", flush=True)
            sys.exit(0)
