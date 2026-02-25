"""deploy-knarr-lite — Deploy and manage knarr nodes in Docker.

Spins up a knarr node container with configurable ports, knarr-mail enabled,
and cockpit accessible on the LAN. Uses the knarr-node Docker image.

Input:
  - name: container name (required, e.g. "architect-node")
  - port: knarr protocol port (default: 9030)
  - cockpit_port: cockpit port (default: 8085)
  - sidecar_port: sidecar port (default: port+1)
  - bootstrap: bootstrap peers (default: bootstrap1.knarr.network:9000)
  - advertise_host: advertise host (required for live deploy)
  - action: "deploy" (default), "status", "stop", "remove", "upgrade"
  - version: knarr version tag for upgrade (e.g. "v0.29.1")

Output:
  - status: ok/error
  - node_id: the deployed node's ID
  - cockpit_token: auto-generated cockpit auth token
  - cockpit_url: full URL to cockpit
  - container_id: Docker container ID
  - container_name: Docker container name
"""

import json
import os
import subprocess
import time
import textwrap

NODE = None

DEFAULT_IMAGE = "knarr-node:latest"
DEFAULT_BOOTSTRAP = "bootstrap1.knarr.network:9000"
DOCKERFILE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "docker", "knarr-node")


def set_node(node):
    global NODE
    NODE = node


def _run(cmd, timeout=30):
    """Run a shell command and return stdout.

    Prefixes with MSYS_NO_PATHCONV=1 to prevent git bash from mangling
    Docker paths on Windows.
    """
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, shell=True, env=env
    )
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def _generate_toml(port, cockpit_port, sidecar_port, bootstrap, advertise_host):
    """Generate a knarr.toml config for the container."""
    return textwrap.dedent(f"""\
        [node]
        host = "0.0.0.0"
        port = {port}
        advertise_host = "{advertise_host}"

        [network]
        bootstrap = ["{bootstrap}"]

        [mail]
        accept_from = "all"

        [cockpit]
        port = {cockpit_port}
        bind = "0.0.0.0"
    """)


def _deploy(input_data: dict) -> dict:
    name = input_data.get("name", "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}

    port = int(input_data.get("port", "9030"))
    cockpit_port = int(input_data.get("cockpit_port", "8085"))
    sidecar_port = int(input_data.get("sidecar_port", str(port + 1)))
    bootstrap = input_data.get("bootstrap", DEFAULT_BOOTSTRAP).strip()
    advertise_host = input_data.get("advertise_host", "").strip()

    if not advertise_host:
        return {"status": "error", "error": "advertise_host is required (your LAN or public IP)"}

    container_name = f"knarr-{name}"

    # Check if container already exists
    rc, out, _ = _run(f'docker ps -a --filter "name=^{container_name}$" --format "{{{{.Status}}}}"')
    if out:
        return {
            "status": "error",
            "error": f"Container '{container_name}' already exists (status: {out}). Use action=remove first.",
        }

    # Generate config
    toml_content = _generate_toml(port, cockpit_port, sidecar_port, bootstrap, advertise_host)

    # Create temp dir for config
    config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "docker-nodes", name)
    os.makedirs(config_dir, exist_ok=True)

    toml_path = os.path.join(config_dir, "knarr.toml")
    with open(toml_path, "w") as f:
        f.write(toml_content)

    # Create persistent data dir (node.db with identity lives here)
    data_dir = os.path.join(config_dir, "data")
    os.makedirs(data_dir, exist_ok=True)

    # Convert Windows paths to Docker-compatible paths
    def _docker_path(p):
        p = p.replace("\\", "/")
        if len(p) > 1 and p[1] == ":":
            p = "/" + p[0].lower() + p[2:]
        return p

    docker_config_dir = _docker_path(config_dir)
    docker_data_dir = _docker_path(data_dir)

    # Run container — mount config AND data dir so identity persists across rebuilds
    docker_cmd = (
        f"docker run -d "
        f"--name {container_name} "
        f"-p 0.0.0.0:{port}:{port} "
        f"-p 0.0.0.0:{cockpit_port}:{cockpit_port} "
        f"-p 0.0.0.0:{sidecar_port}:{sidecar_port} "
        f'-v "{docker_data_dir}:/app" '
        f'-v "{docker_config_dir}:/app/config" '
        f"{DEFAULT_IMAGE} "
        f"knarr serve --config /app/config/knarr.toml"
    )

    rc, container_id, err = _run(docker_cmd, timeout=30)
    if rc != 0:
        return {"status": "error", "error": f"Docker run failed: {err}"}

    container_id = container_id[:12]

    # Wait for cockpit to come up and read the token
    token = ""
    node_id = ""
    for attempt in range(15):
        time.sleep(2)

        # Check container is still running
        rc, state, _ = _run(f"docker inspect --format {{{{{{{{.State.Running}}}}}}}} {container_name}")
        if state != "true":
            rc2, logs, _ = _run(f"docker logs --tail 20 {container_name}")
            return {"status": "error", "error": f"Container stopped. Logs: {logs}"}

        # Try to read the token from host (volume mount) or container
        token_file = os.path.join(config_dir, ".cockpit_token")
        if os.path.exists(token_file):
            with open(token_file) as tf:
                tok = tf.read().strip()
                rc = 0
        else:
            rc, tok, _ = _run(f"docker exec {container_name} cat /app/config/.cockpit_token 2>/dev/null")
        if rc == 0 and tok:
            token = tok.strip()

        # Try to read node ID from logs
        if not node_id:
            rc, logs, _ = _run(f"docker logs {container_name} 2>&1")
            for line in logs.split("\n"):
                if "Node ID:" in line:
                    node_id = line.split("Node ID:")[-1].strip()
                    break

        if token and node_id:
            break

    if not token:
        return {
            "status": "error",
            "error": "Cockpit did not start within 30s. Container is running but token not found.",
            "container_id": container_id,
            "container_name": container_name,
        }

    cockpit_url = f"http://{advertise_host}:{cockpit_port}"

    return {
        "status": "ok",
        "node_id": node_id,
        "cockpit_token": token,
        "cockpit_url": cockpit_url,
        "container_id": container_id,
        "container_name": container_name,
        "port": str(port),
        "cockpit_port": str(cockpit_port),
        "sidecar_port": str(sidecar_port),
    }


def _status(input_data: dict) -> dict:
    name = input_data.get("name", "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}

    container_name = f"knarr-{name}"
    rc, out, _ = _run(f'docker ps -a --filter "name=^{container_name}$" --format "{{{{.Status}}}} | {{{{.Ports}}}}"')
    if not out:
        return {"status": "error", "error": f"Container '{container_name}' not found"}

    return {"status": "ok", "container_name": container_name, "docker_status": out}


def _stop(input_data: dict) -> dict:
    name = input_data.get("name", "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}

    container_name = f"knarr-{name}"
    rc, _, err = _run(f"docker stop {container_name}", timeout=15)
    if rc != 0:
        return {"status": "error", "error": f"Stop failed: {err}"}
    return {"status": "ok", "container_name": container_name, "action": "stopped"}


def _remove(input_data: dict) -> dict:
    name = input_data.get("name", "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}

    container_name = f"knarr-{name}"
    _run(f"docker stop {container_name}", timeout=15)
    rc, _, err = _run(f"docker rm {container_name}", timeout=10)
    if rc != 0:
        return {"status": "error", "error": f"Remove failed: {err}"}
    return {"status": "ok", "container_name": container_name, "action": "removed"}


def _build_image(version: str) -> dict:
    """Build a knarr-node Docker image for a specific version."""
    tag = f"knarr-node:{version}"

    dockerfile = os.path.join(DOCKERFILE_DIR, "Dockerfile")
    if not os.path.isfile(dockerfile):
        return {"status": "error", "error": f"Dockerfile not found at {dockerfile}"}

    with open(dockerfile) as f:
        original = f.read()

    import re
    updated = re.sub(
        r'pip install --no-cache-dir git\+https://github\.com/knarrnet/knarr\.git@[^\s]+',
        f'pip install --no-cache-dir git+https://github.com/knarrnet/knarr.git@{version}',
        original,
    )

    with open(dockerfile, "w") as f:
        f.write(updated)

    try:
        rc, out, err = _run(
            f'docker build -t {tag} -t knarr-node:latest "{DOCKERFILE_DIR}"',
            timeout=180,
        )
    finally:
        with open(dockerfile, "w") as f:
            f.write(original)

    if rc != 0:
        return {"status": "error", "error": f"Image build failed: {err[-500:]}"}

    return {"status": "ok", "image": tag}


def _upgrade(input_data: dict) -> dict:
    """Upgrade a running Docker node to a new knarr version."""
    name = input_data.get("name", "").strip()
    version = input_data.get("version", "").strip()
    if not name:
        return {"status": "error", "error": "name is required"}
    if not version:
        return {"status": "error", "error": "version is required (e.g. v0.29.1)"}

    container_name = f"knarr-{name}"

    rc, state, _ = _run(f'docker ps -a --filter "name=^{container_name}$" --format "{{{{.Status}}}}"')
    if not state:
        return {"status": "error", "error": f"Container '{container_name}' not found"}

    config_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "docker-nodes", name)
    toml_path = os.path.join(config_dir, "knarr.toml")
    port = 9030
    cockpit_port = 8085
    sidecar_port = 9031
    bootstrap = DEFAULT_BOOTSTRAP
    advertise_host = ""

    if os.path.isfile(toml_path):
        with open(toml_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("port") and "cockpit" not in line.lower() and "sidecar" not in line.lower():
                    try:
                        port = int(line.split("=")[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("advertise_host"):
                    advertise_host = line.split("=")[1].strip().strip('"')

        import re as _re
        content = open(toml_path).read()
        m = _re.search(r'\[cockpit\][^\[]*port\s*=\s*(\d+)', content, _re.DOTALL)
        if m:
            cockpit_port = int(m.group(1))
        sidecar_port = port + 1

    build_result = _build_image(version)
    if build_result["status"] != "ok":
        return build_result

    _run(f"docker stop {container_name}", timeout=15)
    _run(f"docker rm {container_name}", timeout=10)

    input_data["action"] = "deploy"
    input_data["port"] = str(port)
    input_data["cockpit_port"] = str(cockpit_port)
    input_data["sidecar_port"] = str(sidecar_port)
    input_data["bootstrap"] = bootstrap
    if advertise_host:
        input_data["advertise_host"] = advertise_host

    result = _deploy(input_data)
    if result.get("status") == "ok":
        result["upgraded_to"] = version
        result["image"] = build_result["image"]

    return result


async def handle(input_data: dict) -> dict:
    action = input_data.get("action", "deploy").strip().lower()

    if action == "deploy":
        return _deploy(input_data)
    elif action == "status":
        return _status(input_data)
    elif action == "stop":
        return _stop(input_data)
    elif action == "remove":
        return _remove(input_data)
    elif action == "upgrade":
        return _upgrade(input_data)
    else:
        return {"status": "error", "error": f"Unknown action: {action}. Use deploy/status/stop/remove/upgrade."}
