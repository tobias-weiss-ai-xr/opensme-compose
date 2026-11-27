"""Docker metrics collection (replaces kubectl collector for Docker Compose).

Provides the same interface as collector.py but uses Docker CLI instead of
kubectl. This allows the predictive-agent to work in Docker Compose environments
without Kubernetes.
"""

import json
import re
import subprocess


def run_cmd(cmd, timeout=30):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def parse_cpu(value):
    """Parse CPU string to millicores. '100m' → 100, '1.5' → 1500."""
    value = value.strip()
    if value.endswith("m"):
        return int(value[:-1])
    try:
        return int(float(value) * 1000)
    except ValueError:
        return 0


def parse_memory(value):
    """Parse memory string to MiB. '128Mi' → 128, '2Gi' → 2048."""
    value = value.strip()
    if value.endswith("Mi"):
        return int(value[:-2])
    elif value.endswith("Gi"):
        return int(value[:-2]) * 1024
    elif value.endswith("Ki"):
        return int(value[:-2]) // 1024
    elif value.endswith("Ti"):
        return int(value[:-2]) * 1024 * 1024
    try:
        return int(value) // (1024 * 1024)  # bytes to MiB
    except ValueError:
        return 0


def docker_available():
    """Check if Docker is available and running."""
    rc, _, _ = run_cmd(["docker", "version", "--format", "json"], timeout=5)
    return rc == 0


def collect_container_stats():
    """Collect container metrics via `docker stats --no-stream --format json`.

    Returns {name: {cpu_m, memory_mib, memory_pct}}.
    """
    rc, stdout, _ = run_cmd(
        ["docker", "stats", "--no-stream", "--format",
         "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}"],
        timeout=15
    )
    if rc != 0 or not stdout:
        return {}

    stats = {}
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 4:
            continue
        name = parts[0]
        cpu_pct = parts[1].rstrip("%").strip()
        mem_usage = parts[2].strip()  # e.g., "100.5MiB / 1GiB"
        mem_pct = parts[3].rstrip("%").strip()

        # Parse memory usage (first value before " / ")
        mem_mib = 0
        if " / " in mem_usage:
            mem_str = mem_usage.split(" / ")[0]
            if mem_str.endswith("MiB"):
                mem_mib = int(float(mem_str[:-3]))
            elif mem_str.endswith("GiB"):
                mem_mib = int(float(mem_str[:-3]) * 1024)
            elif mem_str.endswith("KiB"):
                mem_mib = int(float(mem_str[:-3]) // 1024)

        try:
            cpu_m = int(float(cpu_pct) * 10)  # % to millicores (approx)
        except ValueError:
            cpu_m = 0

        try:
            mem_pct_f = float(mem_pct)
        except ValueError:
            mem_pct_f = 0.0

        stats[name] = {
            "cpu_m": cpu_m,
            "memory_mib": mem_mib,
            "memory_pct": mem_pct_f,
        }
    return stats


def parse_health(status):
    """Derive health from docker ps status text.

    The docker ps health template field is not supported, so health
    must be parsed from the status column, e.g.
    "Up 2 minutes (healthy)" / "(health: starting)" / "(unhealthy)".
    """
    status = status or ""
    if "(healthy)" in status:
        return "healthy"
    if "(unhealthy)" in status:
        return "unhealthy"
    if "health: starting" in status:
        return "starting"
    return ""


def get_containers():
    """List all containers (running and stopped) as JSON-like dicts."""
    rc, stdout, _ = run_cmd(
        ["docker", "ps", "-a", "--format",
         "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}|{{.State}}"],
        timeout=10
    )
    if rc != 0 or not stdout:
        return []

    containers = []
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        cid, name, status, image, state = parts[:5]
        containers.append({
            "id": cid,
            "name": name,
            "status": status,
            "image": image,
            "state": state,
            "health": parse_health(status),
        })
    return containers


def get_container_inspect(name):
    """Inspect a container for detailed state, exit code, OOM, restart count."""
    rc, stdout, _ = run_cmd(
        ["docker", "inspect", "--format",
         "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}|{{.RestartCount}}",
         name],
        timeout=5
    )
    if rc != 0 or not stdout:
        return None
    parts = stdout.split("|")
    if len(parts) < 5:
        return None
    return {
        "status": parts[0],
        "exit_code": parts[1],
        "oom_killed": parts[2].lower() == "true",
        "error": parts[3],
        "restart_count": parts[4],
    }


def get_container_logs(name, tail=50):
    """Fetch recent logs for a container."""
    rc, stdout, _ = run_cmd(["docker", "logs", "--tail", str(tail), name], timeout=10)
    if rc != 0:
        return ""
    return stdout


def get_node_conditions():
    """Get host conditions (Docker has no nodes, but we can report host health).

    Uses /proc/meminfo and /proc/loadavg to approximate node conditions.
    """
    conditions = {}

    # Load average
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
            load1 = float(parts[0])
            load5 = float(parts[1])
            load15 = float(parts[2])
    except Exception:
        load1 = load5 = load15 = 0.0

    # Memory
    mem_total = 0
    mem_available = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_available = int(line.split()[1])
    except Exception:
        pass

    conditions["host"] = {
        "load_1": load1,
        "load_5": load5,
        "load_15": load15,
        "mem_total_mib": mem_total // 1024,
        "mem_available_mib": mem_available // 1024,
        "mem_pct": ((mem_total - mem_available) / mem_total * 100) if mem_total > 0 else 0,
    }
    return conditions


def count_log_errors(log_text):
    """Count error-level log lines."""
    error_patterns = [
        r"\bERROR\b",
        r"\bError\b",
        r"\bFATAL\b",
        r"\bPANIC\b",
        r"\bOOM\b",
        r"\bCrashLoopBackOff\b",
        r"\bException\b",
        r"\bTraceback\b",
        r"\bpanic:",
        r"\bfatal:",
    ]
    count = 0
    for line in log_text.split("\n"):
        for pattern in error_patterns:
            if re.search(pattern, line):
                count += 1
                break
    return count


# ─── Compatibility wrappers for the K8s collector interface ─────────────────
# These match the function names in collector.py so the main loop can use
# either the K8s or Docker collector interchangeably.

def collect_top_metrics(output=None):
    """Docker equivalent: collect container stats.
    The `output` parameter is ignored (Docker gets its own data)."""
    return collect_container_stats()


def collect_top_nodes(output=None):
    """Docker equivalent: collect host conditions."""
    return get_node_conditions()


def get_pod_resources(pod_json):
    """Docker equivalent: get container resource limits from docker inspect."""
    # In Docker, resource limits come from docker inspect, not pod JSON
    # This is a no-op; stats come from collect_container_stats()
    return {}
