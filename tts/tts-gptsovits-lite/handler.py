"""TTS via GPT-SoVITS: GPU-scheduled Docker with V2 API. Blazing fast CJK+EN."""

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

CONTAINER_NAME = "knarr-tts-gptsovits"
IMAGE_NAME = "xxxxrt666/gpt-sovits:latest-cu128"
HOST_PORT = 9880
CONTAINER_PORT = 9880
VRAM_NEEDED_MB = 6000
HEALTH_TIMEOUT = 300
GENERATE_TIMEOUT = 120

# Container-side path where reference audio is mounted
CONTAINER_REF_DIR = "/workspace/ref_audio"


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
            resp = requests.get(f"{base_url}/tts", timeout=5, params={
                "text": "test", "text_lang": "en",
                "ref_audio_path": "", "prompt_lang": "en",
            })
            # A 400 with JSON error means the server is up (just missing ref audio)
            if resp.status_code in {200, 400}:
                return True, f"tts:{resp.status_code}"
            last_err = f"tts:{resp.status_code}"
        except requests.RequestException as exc:
            last_err = str(exc)
        time.sleep(3)
    return False, truncate_text(last_err, 800)


def _resolve_voice_audio(input_data: dict, ctx: Any) -> bytes | None:
    ref = str(input_data.get("voice_ref_asset") or "").strip()
    if ref:
        # knarr auto-resolves knarr-asset:// URIs to local file paths,
        # so ref may be a path like "F:/storage/.../assets/{hash}" or a raw hash.
        p = Path(ref)
        if p.is_file():
            return p.read_bytes()
        # Try as hash (strip knarr-asset:// prefix if present)
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

        text_lang = str(input_data.get("text_lang") or "auto").strip().lower()
        if text_lang not in {"zh", "en", "ja", "ko", "yue", "auto"}:
            text_lang = "auto"
        media_type = str(input_data.get("media_type") or "wav").strip().lower()
        if media_type not in {"wav", "ogg", "aac", "raw"}:
            media_type = "wav"
        speed = max(0.5, min(2.0, float(str(input_data.get("speed") or "1.0"))))
        temperature = max(0.1, min(2.0, float(str(input_data.get("temperature") or "1.0"))))
        host_port = parse_int(str(input_data.get("host_port") or str(HOST_PORT)), HOST_PORT, 1, 65535)

        voice_audio = _resolve_voice_audio(input_data, ctx)
        if not voice_audio:
            raise SkillError("GPT-SoVITS requires voice reference audio (voice_ref_asset or voice_ref_base64)")

        ref_text = str(input_data.get("voice_ref_text") or "").strip()
        ref_lang = str(input_data.get("voice_ref_lang") or text_lang).strip().lower()

        # --- Step 1: GPU allocation ---
        gpu_result = await _call_local("gpu-scheduler-lite", {
            "action": "request",
            "vram_mb": str(input_data.get("vram_mb") or str(VRAM_NEEDED_MB)),
            "gpu_count": "1",
        })

        if str(gpu_result.get("granted")) != "true":
            return ensure_flat_str_dict({
                "status": "gpu_unavailable",
                "engine": "gpt-sovits",
                "reason": str(gpu_result.get("reason", "No GPU with enough VRAM")),
                "retry_after": "30",
            })

        gpu_device = str(gpu_result.get("gpu_device") or "all")

        # --- Step 2: Save reference audio to host dir (mounted into container) ---
        runtime_root = os.getenv("SKILL_RUNTIME_ROOT", "")
        if runtime_root:
            ref_dir = str(Path(runtime_root).resolve() / "tts_voices" / "gptsovits")
        else:
            ref_dir = str(Path(__file__).resolve().parents[1] / "data" / "tts_voices" / "gptsovits")
        Path(ref_dir).mkdir(parents=True, exist_ok=True)

        ref_filename = f"ref_{uuid.uuid4().hex[:12]}.wav"
        ref_host_path = Path(ref_dir) / ref_filename
        ref_host_path.write_bytes(voice_audio)
        ref_container_path = f"{CONTAINER_REF_DIR}/{ref_filename}"

        # --- Step 3: Ensure container ---
        volume_mounts = [f"{ref_dir}:{CONTAINER_REF_DIR}:rw"]

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
            "env_json": json.dumps({"is_half": "true"}),
            "extra_args_json": json.dumps(["--shm-size", "16g"]),
            "command_json": json.dumps([
                "/bin/bash", "-c",
                "rm -rf GPT_SoVITS/pretrained_models GPT_SoVITS/text/G2PWModel && "
                "ln -sf /workspace/models/pretrained_models GPT_SoVITS/pretrained_models && "
                "ln -sf /workspace/models/G2PWModel GPT_SoVITS/text/G2PWModel && "
                f"exec python api_v2.py -a 0.0.0.0 -p {CONTAINER_PORT}",
            ]),
        })
        lifecycle = str(ensure_out.get("lifecycle") or "")

        # --- Step 4: Health probe ---
        base_url = f"http://127.0.0.1:{host_port}"
        health_timeout = parse_int(
            str(input_data.get("health_timeout_secs") or str(HEALTH_TIMEOUT)),
            HEALTH_TIMEOUT, 10, 600,
        )
        healthy, detail = _health_probe(base_url, health_timeout)
        if not healthy:
            raise SkillError(f"GPT-SoVITS not ready after {health_timeout}s: {detail}")

        # --- Step 5: Generate speech via V2 API ---
        gen_timeout = parse_int(
            str(input_data.get("generate_timeout_secs") or str(GENERATE_TIMEOUT)),
            GENERATE_TIMEOUT, 10, 600,
        )

        tts_payload = {
            "text": text,
            "text_lang": text_lang,
            "ref_audio_path": ref_container_path,
            "prompt_text": ref_text,
            "prompt_lang": ref_lang,
            "top_k": 15,
            "top_p": 1.0,
            "temperature": temperature,
            "speed_factor": speed,
            "media_type": media_type,
            "streaming_mode": False,
        }

        resp = requests.post(
            f"{base_url}/tts",
            json=tts_payload,
            timeout=gen_timeout,
        )

        if resp.status_code != 200:
            raise SkillError(f"TTS API error {resp.status_code}: {truncate_text(resp.text, 800)}")

        audio_bytes = resp.content
        if not audio_bytes or len(audio_bytes) < 100:
            raise SkillError("TTS returned empty or invalid audio")

        # --- Step 6: Store in sidecar ---
        asset_hash = NODE.store_asset(audio_bytes)

        # Clean up the temp reference file
        try:
            ref_host_path.unlink(missing_ok=True)
        except Exception:
            pass

        return ensure_flat_str_dict({
            "status": "ok",
            "asset_hash": f"knarr-asset://{asset_hash}",
            "asset_ext": media_type if media_type != "raw" else "pcm",
            "audio_bytes": str(len(audio_bytes)),
            "engine": "gpt-sovits",
            "text_lang": text_lang,
            "cloned": "true",
            "speed": str(speed),
            "gpu_device": gpu_device,
            "container_name": container_name,
            "lifecycle": lifecycle,
        })

    except Exception as exc:
        if lifecycle in {"created_and_started"} and shutdown_mode == "always":
            _stop_container(container_name)
        return error_result(str(exc))
