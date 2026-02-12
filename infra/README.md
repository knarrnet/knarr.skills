# Infrastructure Skills

Low-level primitives for GPU management, Docker container lifecycle, and model operations. These are always private -- called internally by higher-level skills, never exposed to the network.

## Skills

| Skill | Description |
|---|---|
| `gpu-scheduler-lite` | Probe GPU VRAM via pynvml, allocate GPUs with automatic load balancing, evict idle containers |

## GPU Scheduler

The GPU scheduler is the foundation for all GPU-accelerated skills (TTS, image generation, LLM inference). It:

1. Probes all GPUs via pynvml for real-time VRAM usage
2. Picks the GPU with the most free memory
3. Returns a device ID that downstream skills pass to Docker
4. Can evict idle containers to free VRAM when needed

### Actions

- `status` -- Probe all GPUs, list running containers and their VRAM usage
- `request` -- Request a GPU with at least N MB free VRAM
- `evict` -- Force-kill containers to free GPU memory
