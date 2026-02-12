# TTS Voice Synthesis Skills

GPU-scheduled text-to-speech with voice cloning, delivered as a fleet of Docker-containerized engines behind a single public facade.

## Architecture

```
tts-voice-public-lite (PUBLIC facade)
  -> gpu-scheduler-lite (PRIVATE - allocates GPU with VRAM balancing)
  -> docker-container-ensure-lite (PRIVATE - container lifecycle)
  -> tts-qwen3-lite / tts-chatterbox-lite / ... (PRIVATE - engine-specific)
  -> sidecar asset store (binary audio delivery via knarr-asset://)
```

Only `tts-voice-public-lite` is visible on the network. All infrastructure and engine skills are private -- they can only be called locally by other skills on the same provider.

## Skills

| Skill | Engine | Voice Cloning | Strengths | Status |
|---|---|---|---|---|
| `tts-voice-public-lite` | Routes to best available | Yes | Automatic engine selection + fallback | Live |
| `tts-qwen3-lite` | Qwen3-TTS 1.7B | 3s reference audio + presets | Best overall quality, multilingual | Live |
| `tts-chatterbox-lite` | Chatterbox (Resemble AI) | 10s reference audio | Emotion control (exaggeration, cfg_weight) | Live |
| `tts-cosyvoice-lite` | CosyVoice 3 (Alibaba) | Zero-shot cloning | Best Chinese + 18 dialects | Live |
| `tts-gptsovits-lite` | GPT-SoVITS | Required | Blazing fast RTF 0.014, CJK+EN | Deferred (upstream Docker fix needed) |

## Usage

### Basic (auto-routed)

```python
result = await node.call("tts-voice-public-lite", {
    "text": "Hello from the Knarr network!",
})
# result["asset_hash"] -> "knarr-asset://abc123..."
# Fetch audio: GET /assets/{hash} on the provider's sidecar port
```

### With engine preference

```python
result = await node.call("tts-voice-public-lite", {
    "text": "Hello!",
    "engine": "qwen3",       # prefer Qwen3, fallback to others
    "voice": "Chelsie",      # Qwen3 preset voice
})
```

### With voice cloning

```python
result = await node.call("tts-voice-public-lite", {
    "text": "Clone my voice!",
    "voice_ref_asset": "knarr-asset://sha256_of_reference_wav",
    "voice_ref_text": "Transcript of the reference audio",
})
```

### Engine-specific parameters

```python
# Chatterbox emotion control
result = await node.call("tts-voice-public-lite", {
    "text": "This is exciting!",
    "engine": "chatterbox",
    "exaggeration": "1.5",    # 0.25-2.0
    "cfg_weight": "0.3",      # 0.0-1.0
})

# GPT-SoVITS with language and speed
result = await node.call("tts-voice-public-lite", {
    "text": "Speed test",
    "engine": "gptsovits",
    "text_lang": "en",
    "speed": "1.5",
})
```

## Input fields

| Field | Required | Description |
|---|---|---|
| `text` | Yes | Text to synthesize |
| `engine` | No | Preferred engine: `qwen3`, `chatterbox`, `cosyvoice`, `gptsovits` |
| `fallback` | No | Try other engines if preferred is unavailable (default: `true`) |
| `voice` | No | Preset voice name (engine-specific) |
| `voice_ref_asset` | No | `knarr-asset://` hash of reference audio for voice cloning |
| `voice_ref_base64` | No | Base64-encoded reference audio (alternative to asset) |
| `voice_ref_text` | No | Transcript of the reference audio (improves cloning quality) |
| `voice_ref_lang` | No | Language of reference audio (GPT-SoVITS) |
| `text_lang` | No | Target language: `zh`, `en`, `ja`, `ko`, `yue`, `auto` |
| `speed` | No | Speed factor (0.5-2.0) |
| `temperature` | No | Sampling temperature |
| `exaggeration` | No | Emotion exaggeration (Chatterbox, 0.25-2.0) |
| `cfg_weight` | No | Classifier-free guidance weight (Chatterbox, 0.0-1.0) |

## Output fields

| Field | Description |
|---|---|
| `status` | `ok` or error status |
| `asset_hash` | `knarr-asset://` URI for the generated audio |
| `asset_ext` | File extension (`wav`, `opus`, etc.) |
| `audio_bytes` | Size of generated audio in bytes |
| `engine` | Engine that produced the audio |
| `routed_engine` | Engine key used by the facade router |
| `cloned` | `true` if voice cloning was used |
| `gpu_device` | GPU device ID used for generation |

## GPU requirements

Each engine needs ~6-8 GB VRAM. The GPU scheduler automatically picks the GPU with the most free memory. If no GPU has enough headroom, the facade tries the next engine in the fallback chain.

## Installation

```bash
knarr skill install tts-voice-public-lite-1.0.0.knarr
```

Engine skills and infrastructure dependencies must be installed on the same provider for the facade to route to them.
