#!/usr/bin/env python3
"""
openSME Dev Agent v3.1-docker — AI-powered Docker container self-healing operator.

This is the Docker Compose version of the K8s dev-agent. Instead of kubectl,
it uses `docker ps`, `docker inspect`, and `docker stats` to monitor containers.

Reconcile loop:
  1. List all containers (docker ps --format json)
  2. Detect unhealthy containers (exited, restarting, unhealthy, OOMKilled)
  3. Fetch logs (docker logs --tail 50)
  4. Send context to LLM for root-cause analysis
  5. Cache analysis with adaptive TTL (300s → 600s → 1200s)
  6. Expose /healthz, /ready, /metrics, /status, /history, /cache endpoints
"""

import http.server
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request
import urllib.error
from collections import deque

# ─── Configuration ────────────────────────────────────────────────────────────
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "opensme-dev-agent")
OPERATOR_NAMESPACE = os.environ.get("OPERATOR_NAMESPACE", "opensme")
OPERATOR_VERSION = os.environ.get("OPERATOR_VERSION", "3.1.0-docker")
WATCH_NAMESPACES = os.environ.get("OPERATOR_WATCH_NAMESPACES", "opensme,default").split(",")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3-30b-a3b:latest")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "180"))
RECONCILE_INTERVAL = int(os.environ.get("RECONCILE_INTERVAL", "60"))
ANALYSIS_TTL = int(os.environ.get("ANALYSIS_TTL", "300"))
ANALYSIS_TTL_MAX = int(os.environ.get("ANALYSIS_TTL_MAX", "1200"))
MAX_PODS_PER_CYCLE = int(os.environ.get("MAX_PODS_PER_CYCLE", "3"))
LOG_VERBOSITY = os.environ.get("LOG_VERBOSITY", "info")
HISTORY_FILE = os.environ.get("HISTORY_FILE", "/var/lib/opensme/analysis-history.json")
HISTORY_MAX = int(os.environ.get("HISTORY_MAX", "100"))
# Upper bound on in-memory analysis cache entries. Entries are normally
# dropped when they expire on lookup, but keys that are never looked up
# again would otherwise accumulate forever on a long-running agent.
CACHE_MAX_ENTRIES = int(os.environ.get("CACHE_MAX_ENTRIES", "500"))
# All HTTP endpoints (probes + metrics + API) share one server on this port.
METRICS_PORT = int(os.environ.get("OPERATOR_METRICS_BIND_ADDRESS", "0.0.0.0:8080").split(":")[-1])

# LLM backend config
LLM_BACKEND = os.environ.get("LLM_BACKEND", "ollama")
SAIA_API_URL = os.environ.get("SAIA_API_URL", "")
SAIA_API_KEY = os.environ.get("SAIA_API_KEY", "")
SAIA_MODEL = os.environ.get("SAIA_MODEL", "qwen3.5-35b-a3b")
TUD_API_URL = os.environ.get("TUD_API_URL", "")
TUD_API_KEY = os.environ.get("TUD_API_KEY", "")
TUD_MODEL = os.environ.get("TUD_MODEL", "GLM-5.2-AWQ-INT4")
OPENAI_API_URL = os.environ.get("OPENAI_API_URL", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

# Unhealthy container statuses (Docker)
# Note: Docker's health "starting" state is transient during the healthcheck
# start-period — a container still warming up is NOT unhealthy. Flagging it
# fires false-positive LLM analyses on every normal container start, so it is
# deliberately excluded (matches the documented intent: exited, restarting,
# unhealthy, OOMKilled); a container that never becomes healthy transitions
# to "unhealthy" once its start-period elapses and is then caught.
UNHEALTHY_STATUSES = {
    "exited", "restarting", "dead", "removing",
}
UNHEALTHY_HEALTH = {
    "unhealthy",
}

# Statuses where logs are typically empty or unhelpful
SKIP_LOGS_STATUSES = {"created", "restarting", "removing"}

# ─── State ────────────────────────────────────────────────────────────────────
startup_complete = False
ready = False
shutting_down = False
last_reconcile = 0
last_analysis = ""
model_warmup_time = 0
analysis_cache = {}
analysis_history = deque(maxlen=HISTORY_MAX)
metrics = {
    "reconcile_total": 0,
    "errors_total": 0,
    "unhealthy_containers_total": 0,
    "analyses_total": 0,
    "cache_hits_total": 0,
    "cache_misses_total": 0,
    "llm_calls_total": 0,
    "llm_errors_total": 0,
    "model_warmup_seconds": 0,
}


# ─── Docker helpers ───────────────────────────────────────────────────────────
def run_cmd(cmd, timeout=30):
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def docker_available():
    """Check if docker CLI is available and the daemon is running."""
    rc, _, _ = run_cmd(["docker", "version", "--format", "json"], timeout=5)
    return rc == 0


def get_containers():
    """List all containers (running and stopped) as JSON.
    Returns a list of dicts with id, name, status, image, health."""
    rc, stdout, _ = run_cmd(
        ["docker", "ps", "-a", "--format",
         "{{.ID}}|{{.Names}}|{{.Status}}|{{.Image}}|{{.State}}|{{.Health}}"],
        timeout=10
    )
    if rc != 0 or not stdout:
        return []

    containers = []
    for line in stdout.split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) < 6:
            continue
        cid, name, status, image, state, health = parts[:6]
        containers.append({
            "id": cid,
            "name": name,
            "status": status,
            "image": image,
            "state": state,
            "health": health if health != "none" else "",
        })
    return containers


def get_container_logs(name, tail=50):
    """Fetch recent logs for a container."""
    rc, stdout, _ = run_cmd(["docker", "logs", "--tail", str(tail), name], timeout=10)
    if rc != 0:
        return ""
    return stdout


def get_container_inspect(name):
    """Inspect a container for detailed state, exit code, OOM, etc."""
    rc, stdout, _ = run_cmd(["docker", "inspect", "--format",
                             "{{.State.Status}}|{{.State.ExitCode}}|{{.State.OOMKilled}}|{{.State.Error}}|{{.RestartCount}}",
                             name], timeout=5)
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


def is_unhealthy(container):
    """Determine if a container is unhealthy."""
    state = container.get("state", "")
    health = container.get("health", "")
    status = container.get("status", "").lower()

    # Exited, dead, removing
    if state in UNHEALTHY_STATUSES:
        return True
    # Unhealthy health status
    if health in UNHEALTHY_HEALTH:
        return True
    # OOMKilled in status
    if "oomkilled" in status or "out of memory" in status:
        return True
    # Restarting
    if "restart" in status:
        return True
    return False


def should_skip_logs(container):
    """Skip log fetching for certain states."""
    state = container.get("state", "")
    return state in SKIP_LOGS_STATUSES


# ─── LLM analysis ────────────────────────────────────────────────────────────
def call_ollama(prompt, model=None):
    """Call Ollama LLM for analysis. Returns analysis text or empty string."""
    model = model or OLLAMA_MODEL
    url = f"{OLLAMA_URL.rstrip('/')}/api/generate"
    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0,
            "num_predict": 256,
        },
        "format": "json",
    }).encode()
    try:
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT)
        data = json.loads(resp.read())
        return data.get("response", "").strip()
    except Exception as e:
        if LOG_VERBOSITY == "debug":
            print(f"[DEBUG] Ollama error: {e}", flush=True)
        return ""


def call_openai(prompt, api_url=None, api_key=None, model=None):
    """Call an OpenAI-compatible API (LiteLLM, vLLM, SAIA, TUD, OpenAI).

    Works with any endpoint that implements POST /v1/chat/completions or
    POST /chat/completions. This includes:
    - LiteLLM proxy (http://localhost:4000/v1)
    - Local vLLM endpoint (http://localhost:8000/v1)
    - Local llama.cpp endpoint (http://localhost:8080/v1)
    - SAIA, TUD, OpenAI cloud APIs
    """
    api_url = api_url or OPENAI_API_URL
    api_key = api_key or OPENAI_API_KEY
    model = model or OPENAI_MODEL
    endpoint = f"{api_url.rstrip('/')}/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0,
        "max_tokens": 256,
    }).encode()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        req = urllib.request.Request(endpoint, data=payload, headers=headers)
        resp = urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT)
        data = json.loads(resp.read())
        choices = data.get("choices", [])
        if choices:
            return choices[0].get("message", {}).get("content", "").strip()
        return ""
    except Exception as e:
        if LOG_VERBOSITY == "debug":
            print(f"[DEBUG] OpenAI-compatible error: {e}", flush=True)
        return ""


def call_saia(prompt, model=None):
    """Call SAIA LLM API (OpenAI-compatible)."""
    return call_openai(prompt, api_url=SAIA_API_URL, api_key=SAIA_API_KEY, model=model or SAIA_MODEL)


def call_tud(prompt, model=None):
    """Call TUD LLM API (OpenAI-compatible)."""
    return call_openai(prompt, api_url=TUD_API_URL, api_key=TUD_API_KEY, model=model or TUD_MODEL)


def call_llm(prompt, model=None):
    """Dispatch LLM call based on LLM_BACKEND env var.

    Backends:
    - ollama:  Local Ollama (OLLAMA_URL/api/generate)
    - openai:  OpenAI-compatible (OPENAI_API_URL/chat/completions) — works with LiteLLM, vLLM, llama.cpp
    - saia:    SAIA cloud API (OpenAI-compatible)
    - tud:     TUD LLM service (OpenAI-compatible)
    """
    backend = LLM_BACKEND.lower()
    if backend == "ollama":
        return call_ollama(prompt, model)
    elif backend == "openai":
        return call_openai(prompt, model=model)
    elif backend == "saia":
        return call_saia(prompt, model)
    elif backend == "tud":
        return call_tud(prompt, model)
    else:
        # Default to ollama for unknown backends
        return call_ollama(prompt, model)


def build_analysis_prompt(container, logs, inspect_info):
    """Build the LLM prompt for root-cause analysis."""
    name = container.get("name", "unknown")
    image = container.get("image", "unknown")
    status = container.get("status", "unknown")
    state = container.get("state", "unknown")
    health = container.get("health", "")

    inspect_str = ""
    if inspect_info:
        inspect_str = f"""
Exit code: {inspect_info.get('exit_code', 'N/A')}
OOM killed: {inspect_info.get('oom_killed', False)}
Restart count: {inspect_info.get('restart_count', '0')}
Error: {inspect_info.get('error', 'none')}"""

    logs_str = logs[-2000:] if logs else "(no logs available)"

    return f"""You are a container debugging expert. Analyze this unhealthy Docker container and provide a root cause.

Container: {name}
Image: {image}
Status: {status}
State: {state}
Health: {health or 'N/A'}{inspect_str}

Recent logs (last 50 lines):
{logs_str}

Respond in JSON with these fields:
- analysis: brief root cause (1-2 sentences)
- severity: critical | high | medium | low
- action: recommended fix
- command: docker command to fix (or empty)

JSON:"""


def analyze_container(container, logs, inspect_info):
    """Analyze an unhealthy container with LLM. Returns analysis dict or None."""
    prompt = build_analysis_prompt(container, logs, inspect_info)
    analysis_text = call_llm(prompt)
    if not analysis_text:
        return None
    try:
        result = json.loads(analysis_text)
        result["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        result["container"] = container.get("name", "unknown")
        return result
    except json.JSONDecodeError:
        return {"analysis": analysis_text, "severity": "unknown",
                "action": "manual investigation needed", "command": "",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "container": container.get("name", "unknown")}


# ─── Cache ───────────────────────────────────────────────────────────────────
def cache_key(container):
    """Generate a cache key for a container."""
    return f"{container.get('name', 'unknown')}:{container.get('state', 'unknown')}:{container.get('health', '')}"


def get_cached(key):
    """Get cached analysis if within TTL."""
    if key not in analysis_cache:
        return None
    entry = analysis_cache[key]
    age = time.time() - entry["timestamp"]
    if age < entry["ttl"]:
        entry["hits"] = entry.get("hits", 0) + 1
        metrics["cache_hits_total"] += 1
        return entry
    del analysis_cache[key]
    metrics["cache_misses_total"] += 1
    return None


def set_cached(key, analysis):
    """Cache analysis with adaptive TTL."""
    ttl = ANALYSIS_TTL
    if len(analysis_cache) > 10:
        ttl = min(ttl * 2, ANALYSIS_TTL_MAX)
    # Bound memory: once the cache passes its cap, drop expired entries so
    # stale analyses for long-forgotten containers cannot accumulate.
    if len(analysis_cache) >= CACHE_MAX_ENTRIES:
        now = time.time()
        for k in [k for k, e in analysis_cache.items()
                  if now - e.get("timestamp", 0) >= e.get("ttl", 0)]:
            del analysis_cache[k]
    analysis_cache[key] = {
        "timestamp": time.time(),
        "analysis": analysis,
        "ttl": ttl,
        "hits": 0,
    }


# ─── Reconcile loop ──────────────────────────────────────────────────────────
def load_history():
    """Restore persisted analysis history (survives container restarts).

    The history is written after every reconcile; without this load the
    volume would only ever accumulate files that nothing reads back.
    Malformed/legacy files are dropped silently (best effort).
    """
    global analysis_history
    try:
        with open(HISTORY_FILE, "r") as f:
            data = json.load(f)
        if isinstance(data, list):
            analysis_history = deque(data[-HISTORY_MAX:], maxlen=HISTORY_MAX)
            print(f"[INFO] Restored {len(analysis_history)} history entries", flush=True)
    except (OSError, json.JSONDecodeError):
        pass


def reconcile():
    """Main reconcile loop: detect unhealthy containers, analyze with LLM."""
    global last_reconcile, last_analysis

    metrics["reconcile_total"] += 1
    now = time.time()
    last_reconcile = now

    if not docker_available():
        if LOG_VERBOSITY == "debug":
            print("[DEBUG] Docker not available, skipping reconcile", flush=True)
        return

    containers = get_containers()
    unhealthy = [c for c in containers if is_unhealthy(c)]

    # Filter by watch namespaces (container-name substring match,
    # e.g. "opensme" matches containers named "opensme-portal", ...).
    # Note: this is name-based, NOT the compose project name — container
    # names here are "opensme-*", so a project name like "opensme-compose"
    # would match nothing.
    if WATCH_NAMESPACES and WATCH_NAMESPACES != [""]:
        ns_prefixes = [ns.strip() for ns in WATCH_NAMESPACES if ns.strip()]
        if ns_prefixes:
            unhealthy = [c for c in unhealthy
                         if any(p in c.get("name", "") for p in ns_prefixes)]

    metrics["unhealthy_containers_total"] = len(unhealthy)

    if not unhealthy:
        if metrics["reconcile_total"] % 10 == 0:
            print(f"[INFO] All {len(containers)} containers healthy", flush=True)
        return

    # Limit analyses per cycle
    to_analyze = unhealthy[:MAX_PODS_PER_CYCLE]

    for container in to_analyze:
        key = cache_key(container)
        cached = get_cached(key)
        if cached:
            if LOG_VERBOSITY == "debug":
                print(f"[DEBUG] Cache hit for {container['name']}", flush=True)
            continue

        logs = ""
        if not should_skip_logs(container):
            logs = get_container_logs(container["name"])

        inspect_info = get_container_inspect(container["name"])

        analysis = analyze_container(container, logs, inspect_info)
        if analysis:
            set_cached(key, analysis)
            analysis_history.append(analysis)
            last_analysis = analysis.get("analysis", "")
            metrics["analyses_total"] += 1
            metrics["llm_calls_total"] += 1
            print(f"[INFO] Analyzed {container['name']}: {analysis.get('analysis', 'N/A')}", flush=True)
        else:
            metrics["llm_errors_total"] += 1

    # Persist history
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(list(analysis_history), f, indent=2)
    except Exception:
        pass


def reconcile_loop():
    """Background reconcile loop."""
    global startup_complete
    # Warmup the LLM model. Dispatch through call_llm() so the warmup targets
    # the configured backend (ollama | openai | saia | tud) instead of always
    # dialing OLLAMA_URL — previously a saia/tud/openai deployment warmed up
    # (and possibly stalled on) the wrong endpoint before serving probes.
    t0 = time.time()
    try:
        call_llm("Hello")
    except Exception:
        pass
    model_warmup_time = time.time() - t0
    metrics["model_warmup_seconds"] = round(model_warmup_time, 2)
    print(f"[INFO] Model warmup: {model_warmup_time:.1f}s", flush=True)

    startup_complete = True
    while not shutting_down:
        try:
            reconcile()
        except Exception as e:
            metrics["errors_total"] += 1
            print(f"[ERROR] Reconcile error: {e}", flush=True)
        time.sleep(RECONCILE_INTERVAL)


# ─── HTTP server ─────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress access logs

    def _json(self, data, code=200):
        body = json.dumps(data, indent=2, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/healthz":
            self._json({"status": "ok" if startup_complete else "starting"})
        elif self.path == "/ready":
            self._json({"ready": ready})
        elif self.path == "/metrics":
            self._json({
                "metrics": metrics,
                "cache_size": len(analysis_cache),
                "history_size": len(analysis_history),
            })
        elif self.path == "/status":
            self._json({
                "operator": OPERATOR_NAME,
                "version": OPERATOR_VERSION,
                "uptime_seconds": round(time.time() - _start_time, 1),
                "last_reconcile": last_reconcile,
                "model_warmup_seconds": metrics.get("model_warmup_seconds", 0),
                "cache_size": len(analysis_cache),
                "history_size": len(analysis_history),
                "docker_available": docker_available(),
            })
        elif self.path == "/history":
            self._json(list(analysis_history))
        elif self.path == "/cache":
            self._json(list(analysis_cache.values()))
        else:
            self._json({"error": "not found"}, 404)


def start_server():
    """Start the HTTP server in a background thread."""
    global ready
    # Threading server: /status (and /metrics) run blocking `docker` CLI
    # calls; on a single-threaded server one slow request would stall the
    # /healthz and /ready probes past the compose healthcheck timeout.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ready = True
    print(f"[INFO] Metrics server on :{METRICS_PORT}", flush=True)
    return server


# ─── Main ────────────────────────────────────────────────────────────────────
_start_time = time.time()


def signal_handler(signum, frame):
    global shutting_down
    shutting_down = True
    print(f"[INFO] Received signal {signum}, shutting down...", flush=True)


def main():
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    print(f"[INFO] === {OPERATOR_NAME} v{OPERATOR_VERSION} starting ===", flush=True)
    print(f"[INFO] Docker available: {docker_available()}", flush=True)
    print(f"[INFO] LLM: {LLM_BACKEND} / {OLLAMA_MODEL}", flush=True)
    print(f"[INFO] Reconcile interval: {RECONCILE_INTERVAL}s", flush=True)

    # Restore persisted history (volume-backed) before serving
    load_history()

    # Start metrics server
    server = start_server()

    # Start reconcile loop (blocks until shutdown)
    reconcile_loop()

    server.shutdown()
    print("[INFO] Shutdown complete", flush=True)


if __name__ == "__main__":
    main()
