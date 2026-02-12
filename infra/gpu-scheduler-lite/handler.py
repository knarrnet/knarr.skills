"""GPU scheduler: probe VRAM, check running containers, grant/deny GPU access."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, parse_int, truncate_text


def _gpu_status() -> List[Dict[str, Any]]:
    """Probe all GPUs via NVML. Returns list of per-GPU dicts."""
    try:
        import pynvml as nvml
    except ImportError:
        import nvidia_ml_py as nvml
    nvml.nvmlInit()
    try:
        gpus = []
        for i in range(nvml.nvmlDeviceGetCount()):
            h = nvml.nvmlDeviceGetHandleByIndex(i)
            mem = nvml.nvmlDeviceGetMemoryInfo(h)
            util = nvml.nvmlDeviceGetUtilizationRates(h)
            gpus.append({
                "index": i,
                "name": nvml.nvmlDeviceGetName(h),
                "vram_total_mb": mem.total // (1024 * 1024),
                "vram_used_mb": mem.used // (1024 * 1024),
                "vram_free_mb": mem.free // (1024 * 1024),
                "gpu_util_pct": util.gpu,
                "mem_util_pct": util.memory,
            })
        return gpus
    finally:
        nvml.nvmlShutdown()


def _running_gpu_containers() -> List[Dict[str, str]]:
    """List running Docker containers that have GPU access."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return []
    try:
        proc = subprocess.run(
            [docker_bin, "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return []
        containers = []
        for line in (proc.stdout or "").strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                containers.append({
                    "name": parts[0],
                    "image": parts[1],
                    "status": parts[2] if len(parts) > 2 else "",
                })
        return containers
    except Exception:
        return []


def _stop_container(name: str) -> bool:
    """Stop and remove a Docker container. Returns True on success."""
    docker_bin = shutil.which("docker")
    if not docker_bin:
        return False
    try:
        subprocess.run(
            [docker_bin, "rm", "-f", name],
            capture_output=True, text=True, timeout=30,
        )
        return True
    except Exception:
        return False


def _select_gpu(gpus: List[Dict[str, Any]], vram_needed_mb: int, gpu_count: int) -> Dict[str, Any]:
    """Select the best GPU(s) for the requested workload.

    Returns {"granted": True, "gpu_device": "0", ...} or {"granted": False, "reason": ...}
    """
    if gpu_count >= 2:
        # Need all GPUs — check total free VRAM
        total_free = sum(g["vram_free_mb"] for g in gpus)
        if total_free >= vram_needed_mb:
            return {
                "granted": True,
                "gpu_device": "all",
                "gpu_indices": [g["index"] for g in gpus],
                "total_free_mb": total_free,
            }
        return {
            "granted": False,
            "reason": f"Need {vram_needed_mb}MB across {gpu_count} GPUs, only {total_free}MB free total",
            "total_free_mb": total_free,
        }

    # Single GPU — pick the one with most free VRAM that satisfies the request
    candidates = sorted(gpus, key=lambda g: g["vram_free_mb"], reverse=True)
    for gpu in candidates:
        if gpu["vram_free_mb"] >= vram_needed_mb:
            return {
                "granted": True,
                "gpu_device": str(gpu["index"]),
                "gpu_indices": [gpu["index"]],
                "vram_free_mb": gpu["vram_free_mb"],
            }

    best = candidates[0] if candidates else None
    if best:
        return {
            "granted": False,
            "reason": f"Need {vram_needed_mb}MB, best GPU has {best['vram_free_mb']}MB free (GPU {best['index']})",
            "best_gpu": best["index"],
            "best_free_mb": best["vram_free_mb"],
        }
    return {"granted": False, "reason": "No GPUs found"}


async def handle(input_data: dict) -> dict:
    try:
        action = str(input_data.get("action") or "status").strip().lower()
        if action not in {"status", "request", "evict"}:
            raise SkillError("Invalid action. Use: status, request, evict")

        gpus = _gpu_status()
        containers = _running_gpu_containers()

        if action == "status":
            # Pure probe — return GPU state + running containers
            return ensure_flat_str_dict({
                "status": "ok",
                "gpu_count": str(len(gpus)),
                "gpus_json": json.dumps(gpus, separators=(",", ":")),
                "containers_json": json.dumps(containers, separators=(",", ":")),
                "total_vram_free_mb": str(sum(g["vram_free_mb"] for g in gpus)),
                "total_vram_used_mb": str(sum(g["vram_used_mb"] for g in gpus)),
            })

        if action == "request":
            vram_needed_mb = parse_int(
                str(input_data.get("vram_mb") or "10000"), 10000, min_value=512, max_value=50000
            )
            gpu_count = parse_int(
                str(input_data.get("gpu_count") or "1"), 1, min_value=1, max_value=2
            )
            evict_idle = str(input_data.get("evict_idle") or "false").strip().lower() in ("true", "1", "yes")

            result = _select_gpu(gpus, vram_needed_mb, gpu_count)

            if not result["granted"] and evict_idle and containers:
                # Try evicting idle containers to free VRAM
                evicted = []
                for c in containers:
                    name = c["name"]
                    if _stop_container(name):
                        evicted.append(name)

                if evicted:
                    # Re-probe after eviction
                    import time
                    time.sleep(2)
                    gpus = _gpu_status()
                    result = _select_gpu(gpus, vram_needed_mb, gpu_count)
                    result["evicted"] = evicted

            return ensure_flat_str_dict({
                "status": "ok",
                "granted": "true" if result["granted"] else "false",
                "gpu_device": str(result.get("gpu_device", "")),
                "reason": str(result.get("reason", "")),
                "vram_needed_mb": str(vram_needed_mb),
                "gpu_count": str(gpu_count),
                "gpus_json": json.dumps(gpus, separators=(",", ":")),
                "evicted_json": json.dumps(result.get("evicted", []), separators=(",", ":")),
                "retry_after": "30" if not result["granted"] else "",
            })

        if action == "evict":
            # Force-stop a named container
            target = str(input_data.get("container_name") or "").strip()
            if not target:
                raise SkillError("evict requires container_name")
            ok = _stop_container(target)
            return ensure_flat_str_dict({
                "status": "ok" if ok else "evict_failed",
                "container_name": target,
                "evicted": "true" if ok else "false",
            })

    except Exception as exc:
        return error_result(str(exc))
