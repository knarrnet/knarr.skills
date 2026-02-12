"""TTS via CosyVoice 3: GPU-scheduled Docker with OpenAI-compatible API (neosun wrapper)."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import requests

sys.path.insert(0, os.path.dirname(__file__))
from _common import SkillError, ensure_flat_str_dict, error_result, parse_int, truncate_text

NODE: Any = None

CONTAINER_NAME = "knarr-tts-cosyvoice"
IMAGE_NAME = "neosun/cosyvoice:latest"
HOST_PORT = 8189
CONTAINER_PORT = 8188
VRAM_NEEDED_MB = 8000
HEALTH_TIMEOUT = 180
GENERATE_TIMEOUT = 120


def set_node(node: Any) -> None:
    global NODE
    NODE = node


async def _call_local(skill: str, payload: Dict[str, Any]) -> Dict[str, str]:
    if NODE is None:
        raise SkillError("Skill not initialized with node context")
    out = await NODE.call_local(skill, payload)
    if "error" in out:
        raise SkillError(f"{skill} failed: {out.get('error', 'unknown error')}")
    return out


def _health_probe(base_url: str, timeout_secs: int) -> tuple[bool, str]:
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

        voice = str(input_data.get("voice") or "").strip()
        host_port = parse_int(str(input_data.get("host_port") or str(HOST_PORT)), HOST_PORT, 1, 65535)

        voice_audio = _resolve_voice_audio(input_data, ctx)
        use_cloning = voice_audio is not None
        if not use_cloning and not voice:
            raise SkillError("CosyVoice requires voice reference audio (voice_ref_asset or voice_ref_base64) or a pre-registered voice ID")
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
                "engine": "cosyvoice",
                "reason": str(gpu_result.get("reason", "No GPU with enough VRAM")),
                "retry_after": "30",
            })

        gpu_device = str(gpu_result.get("gpu_device") or "all")

        # --- Step 2: Ensure container (neosun/cosyvoice with OpenAI-compatible API) ---
        runtime_root = os.getenv("SKILL_RUNTIME_ROOT", "")
        if runtime_root:
            voices_dir = str(Path(runtime_root).resolve() / "tts_voices" / "cosyvoice")
        else:
            voices_dir = str(Path(__file__).resolve().parents[1] / "data" / "tts_voices" / "cosyvoice")
        Path(voices_dir).mkdir(parents=True, exist_ok=True)

        volume_mounts = [f"{voices_dir}:/data/voices:rw"]

        ensure_out = await _call_local("docker-container-ensure-lite", {
            "container_name": container_name,
            "image": str(input_data.get("image") or IMAGE_NAME),
            "host_port": str(host_port),
            "container_port": str(CONTAINER_PORT),
            "use_gpu": "true",
            "gpu_device": gpu_device,
            "pull_image": str(input_data.get("pull_image") or "true"),
            "force_recreate": "false",
            "wait_for_health": "false",
            "timeout_secs": "120",
            "volume_mounts_json": json.dumps(volume_mounts),
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
            raise SkillError(f"CosyVoice not ready after {health_timeout}s: {detail}")

        # --- Step 4: Voice cloning registration if reference provided ---
        clone_voice_id = ""
        if use_cloning:
            clone_voice_name = f"ref_{uuid.uuid4().hex[:12]}"
            resp = requests.post(
                f"{base_url}/v1/voices/create",
                files={"audio": (f"{clone_voice_name}.wav", voice_audio, "audio/wav")},
                data={"name": clone_voice_name, "text": ref_text} if ref_text else {"name": clone_voice_name},
                timeout=30,
            )
            if resp.status_code not in {200, 201}:
                raise SkillError(f"Voice registration failed {resp.status_code}: {truncate_text(resp.text, 400)}")
            try:
                rj = resp.json()
                clone_voice_id = rj.get("voice_id") or rj.get("id") or clone_voice_name
            except Exception:
                clone_voice_id = clone_voice_name
            voice = str(clone_voice_id)

        # --- Step 5: Generate speech via OpenAI-compatible endpoint ---
        gen_timeout = parse_int(
            str(input_data.get("generate_timeout_secs") or str(GENERATE_TIMEOUT)),
            GENERATE_TIMEOUT, 10, 600,
        )

        speech_payload: Dict[str, Any] = {"input": text}
        if voice:
            speech_payload["voice"] = voice

        resp = requests.post(
            f"{base_url}/v1/audio/speech",
            json=speech_payload,
            timeout=gen_timeout,
        )

        if resp.status_code != 200:
            raise SkillError(f"TTS API error {resp.status_code}: {truncate_text(resp.text, 800)}")

        audio_bytes = resp.content
        if not audio_bytes or len(audio_bytes) < 100:
            raise SkillError("TTS returned empty or invalid audio")

        # --- Step 6: Store in sidecar ---
        asset_hash = NODE.store_asset(audio_bytes)

        return ensure_flat_str_dict({
            "status": "ok",
            "asset_hash": f"knarr-asset://{asset_hash}",
            "asset_ext": "wav",
            "audio_bytes": str(len(audio_bytes)),
            "engine": "cosyvoice",
            "voice": voice or "default",
            "cloned": "true" if use_cloning else "false",
            "gpu_device": gpu_device,
            "container_name": container_name,
            "lifecycle": lifecycle,
        })

    except Exception as exc:
        if lifecycle in {"created_and_started"} and shutdown_mode == "always":
            _stop_container(container_name)
        return error_result(str(exc))
