"""TTS via Qwen3-TTS: GPU-scheduled Docker container with OpenAI-compatible API."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict

import requests

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, parse_int, truncate_text

NODE: Any = None

CONTAINER_NAME = "knarr-tts-qwen3"
IMAGE_NAME = "qwen3-tts-api:latest"
HOST_PORT = 8880
CONTAINER_PORT = 8880
VRAM_NEEDED_MB = 6000
HEALTH_TIMEOUT = 180
GENERATE_TIMEOUT = 120

# Preset voice aliases (OpenAI -> Qwen3)
VOICE_ALIASES = {
    "alloy": "Vivian", "echo": "Ryan", "fable": "Sophia",
    "nova": "Isabella", "onyx": "Evan", "shimmer": "Lily",
}


def set_node(node: Any) -> None:
    global NODE
    NODE = node


def _parse_bool(raw: Any, default: bool = False) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    return text in {"1", "true", "yes", "y", "on"} if text not in {"0", "false", "no", "n", "off"} else False


async def _call_local(skill: str, payload: Dict[str, Any]) -> Dict[str, str]:
    if NODE is None:
        raise SkillError("Skill not initialized with node context")
    out = await NODE.call_local(skill, payload)
    if "error" in out:
        raise SkillError(f"{skill} failed: {out.get('error', 'unknown error')}")
    return out


def _health_probe(base_url: str, timeout_secs: int) -> tuple[bool, str]:
    """Poll /health until the TTS server is ready."""
    deadline = time.time() + max(1, timeout_secs)
    last_err = "no response"
    while time.time() < deadline:
        try:
            resp = requests.get(f"{base_url}/health", timeout=5)
            if 200 <= resp.status_code < 400:
                return True, f"health:{resp.status_code}"
            last_err = f"health:{resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(3)
    return False, truncate_text(last_err, 800)


def _resolve_voice_audio(input_data: dict, ctx: Any) -> bytes | None:
    """Resolve voice reference audio from asset URI, base64, or file path."""
    ref = str(input_data.get("voice_ref_asset") or "").strip()
    if ref:
        # knarr auto-resolves knarr-asset:// URIs to local file paths
        p = Path(ref)
        if p.is_file():
            return p.read_bytes()
        asset_hash = ref[len("knarr-asset://"):] if ref.startswith("knarr-asset://") else ref
        if NODE is not None:
            try:
                return NODE.get_asset(asset_hash)
            except Exception:
                pass
        if ctx is not None:
            try:
                return ctx.get_asset(asset_hash)
            except Exception:
                pass
        raise SkillError(f"Voice ref asset not found: {ref[:40]}...")

    # Check base64
    b64 = str(input_data.get("voice_ref_base64") or "").strip()
    if b64:
        try:
            return base64.b64decode(b64)
        except Exception as exc:
            raise SkillError(f"Invalid voice_ref_base64: {exc}")

    return None


def _stop_container(name: str) -> None:
    docker_bin = shutil.which("docker")
    if docker_bin:
        try:
            subprocess.run([docker_bin, "rm", "-f", name], capture_output=True, text=True, timeout=30)
        except Exception:
            pass


async def handle(input_data: dict, ctx=None) -> dict:
    lifecycle = ""
    container_name = str(input_data.get("container_name") or CONTAINER_NAME).strip()
    shutdown_mode = str(input_data.get("shutdown_mode") or "never").strip().lower()

    try:
        if NODE is None:
            raise SkillError("Skill not initialized with node context")

        text = str(input_data.get("text") or "").strip()
        if not text:
            raise SkillError("Missing required field: text")

        voice = str(input_data.get("voice") or "Vivian").strip()
        voice = VOICE_ALIASES.get(voice.lower(), voice)
        model = str(input_data.get("model") or "qwen3-tts").strip()
        response_format = str(input_data.get("response_format") or "wav").strip().lower()
        if response_format not in {"mp3", "wav", "opus", "flac", "aac", "pcm"}:
            response_format = "wav"
        speed = max(0.25, min(4.0, float(str(input_data.get("speed") or "1.0"))))
        host_port = parse_int(str(input_data.get("host_port") or str(HOST_PORT)), HOST_PORT, 1, 65535)

        # Resolve voice reference audio for cloning (optional)
        voice_audio = _resolve_voice_audio(input_data, ctx)
        use_cloning = voice_audio is not None
        ref_text = str(input_data.get("voice_ref_text") or "").strip()

        # --- Step 1: GPU allocation ---
        gpu_result = await _call_local("gpu-scheduler-lite", {
            "action": "request",
            "vram_mb": str(input_data.get("vram_mb") or str(VRAM_NEEDED_MB)),
            "gpu_count": "1",
        })

        if str(gpu_result.get("granted")) != "true":
            return ensure_flat_str_dict({
                "status": "gpu_unavailable",
                "engine": "qwen3-tts",
                "reason": str(gpu_result.get("reason", "No GPU with enough VRAM")),
                "retry_after": "30",
            })

        gpu_device = str(gpu_result.get("gpu_device") or "all")

        # --- Step 2: Ensure container ---
        runtime_root = os.getenv("SKILL_RUNTIME_ROOT", "")
        if runtime_root:
            hf_cache = str(Path(runtime_root).resolve() / "hf_cache")
        else:
            hf_cache = str(Path(__file__).resolve().parents[1] / "data" / "hf_cache")
        Path(hf_cache).mkdir(parents=True, exist_ok=True)

        volume_mounts = [f"{hf_cache}:/root/.cache/huggingface:rw"]
        env_vars = {
            "TTS_BACKEND": "official",
            "TTS_WARMUP_ON_START": "true",
            "HOST": "0.0.0.0",
            "PORT": str(CONTAINER_PORT),
            "WORKERS": "1",
        }

        ensure_out = await _call_local("docker-container-ensure-lite", {
            "container_name": container_name,
            "image": str(input_data.get("image") or IMAGE_NAME),
            "host_port": str(host_port),
            "container_port": str(CONTAINER_PORT),
            "use_gpu": "true",
            "gpu_device": gpu_device,
            "pull_image": "false",
            "force_recreate": "false",
            "wait_for_health": "false",
            "timeout_secs": "120",
            "volume_mounts_json": json.dumps(volume_mounts),
            "env_json": json.dumps(env_vars),
        })
        lifecycle = str(ensure_out.get("lifecycle") or "")

        # --- Step 3: Health probe ---
        base_url = f"http://127.0.0.1:{host_port}"
        health_timeout = parse_int(
            str(input_data.get("health_timeout_secs") or str(HEALTH_TIMEOUT)),
            HEALTH_TIMEOUT, 10, 600,
        )
        healthy, detail = _health_probe(base_url, health_timeout)
        if not healthy:
            raise SkillError(f"Qwen3-TTS not ready after {health_timeout}s: {detail}")

        # --- Step 4: Generate speech ---
        gen_timeout = parse_int(
            str(input_data.get("generate_timeout_secs") or str(GENERATE_TIMEOUT)),
            GENERATE_TIMEOUT, 10, 600,
        )

        if use_cloning:
            # Voice cloning via /audio/voice-clone
            audio_b64 = base64.b64encode(voice_audio).decode("ascii")
            clone_payload: Dict[str, Any] = {
                "text": text,
                "reference_audio": audio_b64,
            }
            if ref_text:
                clone_payload["ref_text"] = ref_text
            resp = requests.post(
                f"{base_url}/audio/voice-clone",
                json=clone_payload,
                timeout=gen_timeout,
            )
        else:
            # Preset voice via OpenAI-compatible endpoint
            resp = requests.post(
                f"{base_url}/v1/audio/speech",
                json={
                    "model": model,
                    "input": text,
                    "voice": voice,
                    "response_format": response_format,
                    "speed": speed,
                },
                timeout=gen_timeout,
            )

        if resp.status_code != 200:
            raise SkillError(f"TTS API error {resp.status_code}: {truncate_text(resp.text, 800)}")

        audio_bytes = resp.content
        if not audio_bytes or len(audio_bytes) < 100:
            raise SkillError("TTS returned empty or invalid audio")

        # --- Step 5: Store in sidecar ---
        asset_hash = NODE.store_asset(audio_bytes)
        asset_ext = response_format if not use_cloning else "wav"

        return ensure_flat_str_dict({
            "status": "ok",
            "asset_hash": f"knarr-asset://{asset_hash}",
            "asset_ext": asset_ext,
            "audio_bytes": str(len(audio_bytes)),
            "engine": "qwen3-tts",
            "model": model,
            "voice": voice if not use_cloning else "cloned",
            "cloned": "true" if use_cloning else "false",
            "response_format": response_format,
            "gpu_device": gpu_device,
            "container_name": container_name,
            "lifecycle": lifecycle,
        })

    except Exception as exc:
        if lifecycle in {"created_and_started"} and shutdown_mode == "always":
            _stop_container(container_name)
        return error_result(str(exc))
