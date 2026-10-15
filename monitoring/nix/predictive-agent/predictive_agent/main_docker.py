#!/usr/bin/env python3
"""openSME Predictive Agent v4.0-docker — Docker reconcile loop.

Docker-native version of main.py. Uses docker_collector (docker ps, docker
stats, docker inspect, docker logs) instead of kubectl. Maintains the same
state model (Kalman filters, Markov chains, Bayesian risk scoring) and HTTP
endpoints as the K8s version.

Environment variables:
  COLLECTOR_MODE=docker  (required for Docker mode)
  LLM_BACKEND=ollama|openai|saia|tud
  OLLAMA_URL, OLLAMA_MODEL, OPENAI_API_URL, OPENAI_API_KEY, OPENAI_MODEL, etc.
  RECONCILE_INTERVAL=60
  PREDICTION_ENABLED=true
  PREDICTION_RISK_THRESHOLD=0.5
"""

import json
import logging
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from predictive_agent import config
from predictive_agent.docker_collector import (
    collect_container_stats,
    get_container_inspect,
    get_container_logs,
    get_containers,
    get_node_conditions,
    count_log_errors,
    docker_available,
)
from predictive_agent.llm import LLMAnalyzer, LLMBackend
from predictive_agent.persistence import StateStore
from predictive_agent.predictor import Predictor
from predictive_agent.state_model import StateModel

logger = logging.getLogger("predictive-agent-docker")

# ─── Global state (shared with server.py) ──────────────────────────────────
_state_model: Optional[StateModel] = None
_predictor: Optional[Predictor] = None
_state_store: Optional[StateStore] = None
_cache: Dict[str, Any] = {}
_history: list = []
_reconcile_count = 0
_last_reconcile_time: Optional[str] = None
_server = None
_llm: Optional[LLMAnalyzer] = None


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _docker_available() -> bool:
    """Check if Docker is available."""
    return docker_available()


def _get_container_memory_limit(container_name: str) -> int:
    """Get container memory limit in MiB from docker inspect."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.HostConfig.Memory}}", container_name],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 0
    try:
        mem_bytes = int(result.stdout.strip())
        if mem_bytes == 0:
            return 0
        return mem_bytes // (1024 * 1024)  # bytes to MiB
    except ValueError:
        return 0


def _should_watch_container(name: str) -> bool:
    """Check if a container should be watched based on WATCH_NAMESPACES.

    In Docker mode, 'namespaces' map to container name prefixes.
    E.g., WATCH_NAMESPACES=opensme matches containers starting
    with 'opensme-'.
    """
    if not config.WATCH_NAMESPACES or config.WATCH_NAMESPACES == [""]:
        return True
    for ns in config.WATCH_NAMESPACES:
        ns = ns.strip()
        if ns and name.startswith(ns.replace("-", "-")):
            return True
        # Also check if the namespace string appears in the container name
        if ns and ns in name:
            return True
    return False


def reconcile() -> Dict[str, Any]:
    """Run one reconcile cycle using Docker CLI.

    Collects container stats via `docker stats`, updates state model,
    generates predictions, persists state, and returns a summary dict.
    """
    global _reconcile_count, _last_reconcile_time

    _reconcile_count += 1
    cycle = _reconcile_count
    logger.info("Reconcile #%d: starting (Docker mode)", cycle)

    if not _docker_available():
        logger.warning("Docker not available, skipping reconcile")
        return {
            "predictions": [],
            "state": {},
            "timestamp": _now_iso(),
            "containers_tracked": 0,
            "at_risk_count": 0,
            "reconcile_count": cycle,
        }

    # ─── Collect metrics ───────────────────────────────────────────────
    container_stats = collect_container_stats()  # {name: {cpu_m, memory_mib, memory_pct}}
    containers = get_containers()  # [{id, name, status, image, state, health}]
    node_conditions = get_node_conditions()  # {host: {load_1, load_5, ...}}

    # ─── Update state model ────────────────────────────────────────────
    if _state_model is None:
        logger.warning("State model not initialized")
        return {
            "predictions": [],
            "state": {},
            "timestamp": _now_iso(),
            "containers_tracked": 0,
            "at_risk_count": 0,
            "reconcile_count": cycle,
        }

    containers_tracked = 0
    at_risk_count = 0

    for container in containers:
        name = container.get("name", "")
        state = container.get("state", "")
        health = container.get("health", "")

        if not _should_watch_container(name):
            continue

        # Skip healthy running containers from detailed analysis
        # but still track them in the state model
        stats = container_stats.get(name, {})
        cpu_m = stats.get("cpu_m", 0)
        memory_mib = stats.get("memory_mib", 0)
        memory_pct = stats.get("memory_pct", 0.0)

        # Get memory limit from docker inspect
        memory_limit_mib = _get_container_memory_limit(name)

        # Get restart count and OOM status from docker inspect
        inspect_info = get_container_inspect(name)
        restart_count = 0
        oom_killed = False
        if inspect_info:
            try:
                restart_count = int(inspect_info.get("restart_count", 0))
            except (ValueError, TypeError):
                restart_count = 0
            oom_killed = inspect_info.get("oom_killed", False)

        # Collect log errors for unhealthy containers
        log_errors = 0
        is_unhealthy = (
            state in ("exited", "restarting", "dead", "removing")
            or health in ("unhealthy", "starting")
            or oom_killed
        )
        if is_unhealthy:
            logs = get_container_logs(name, tail=50)
            log_errors = count_log_errors(logs)

        # Determine Markov state from container health
        if state == "running" and health in ("", "healthy"):
            markov_state = "HEALTHY"
        elif state == "running" and health == "starting":
            markov_state = "DEGRADED"
        elif state == "exited" or state == "dead":
            markov_state = "FAILED"
        elif state == "restarting":
            markov_state = "CRITICAL"
        elif oom_killed:
            markov_state = "CRITICAL"
        elif memory_pct > 90 or cpu_m > 8000:
            markov_state = "STRESSED"
        else:
            markov_state = "HEALTHY"

        # Update state model
        tracker = _state_model.update_pod(
            namespace="docker",
            name=name,
            memory_mib=memory_mib,
            memory_limit_mib=memory_limit_mib,
            cpu_m=cpu_m,
            restart_count=restart_count,
            log_errors=log_errors,
            node_pressure=False,  # Docker doesn't have node pressure
        )
        containers_tracked += 1

        # Generate prediction
        if _predictor is not None:
            markov_p_critical = 0.0
            markov_p_failed = 0.0
            if _state_model.markov:
                transitions = _state_model.markov.predict(markov_state, steps=1)
                markov_p_critical = transitions.get("CRITICAL", 0.0)
                markov_p_failed = transitions.get("FAILED", 0.0)

            restart_rate = restart_count  # Approximate: restarts per cycle
            log_error_rate = log_errors / 60.0  # Approximate per-minute rate

            result = _predictor.predict(
                pod_key=f"docker/{name}",
                memory_pct=memory_pct,
                memory_trend_mib_per_min=tracker.memory_trend,
                memory_limit_mib=memory_limit_mib,
                memory_mib=memory_mib,
                cpu_pct=tracker.cpu_pct,
                restart_rate_per_hr=restart_rate,
                log_error_rate_per_min=log_error_rate,
                node_memory_pressure=False,
                node_disk_pressure=False,
                markov_state=markov_state,
                markov_p_critical=markov_p_critical,
                markov_p_failed=markov_p_failed,
            )

            if result.risk_score >= _predictor.risk_threshold:
                at_risk_count += 1
                logger.warning(
                    "Container %s at risk: score=%.2f ttf=%s state=%s",
                    name, result.risk_score, result.ttf_minutes, markov_state,
                )

                # LLM analysis for at-risk containers
                if _llm is not None and config.PREDICTION_ENABLED:
                    _analyze_with_llm(name, container, logs if is_unhealthy else "", result, markov_state)

    # Update Markov chain
    if _state_model.markov:
        for pod_key, tracker in _state_model.pods.items():
            _state_model.markov.record_transition(tracker.prev_state, tracker.state)

    # ─── Persist state ─────────────────────────────────────────────────
    if _state_store is not None:
        try:
            _state_store.save_markov(_state_model.markov)
            if _predictor is not None:
                predictions_data = [
                    {
                        "pod_key": p.pod_key,
                        "risk_score": p.risk_score,
                        "ttf_minutes": p.ttf_minutes,
                        "confidence": p.confidence,
                        "markov_state": p.markov_state,
                        "memory_trend": p.memory_trend,
                        "cpu_trend": p.cpu_trend,
                        "memory_pct": p.memory_pct,
                        "cpu_pct": p.cpu_pct,
                    }
                    for p in _predictor._predictions.values()
                ]
                _state_store.save_predictions(predictions_data)
        except Exception as e:
            logger.error("Failed to persist state: %s", e)

    _last_reconcile_time = _now_iso()

    result = {
        "predictions": [
            {
                "pod_key": p.pod_key,
                "risk_score": p.risk_score,
                "ttf_minutes": p.ttf_minutes,
            }
            for p in (_predictor._predictions.values() if _predictor else [])
        ],
        "state": _state_model.to_dict() if _state_model else {},
        "timestamp": _last_reconcile_time,
        "containers_tracked": containers_tracked,
        "at_risk_count": at_risk_count,
        "reconcile_count": cycle,
    }

    if at_risk_count > 0:
        logger.warning("Reconcile #%d: %d containers at risk", cycle, at_risk_count)
    else:
        logger.info("Reconcile #%d: %d containers tracked, 0 at risk", cycle, containers_tracked)

    return result


def _analyze_with_llm(name, container, logs, prediction, markov_state):
    """Analyze an at-risk container with LLM."""
    try:
        issue = f"Container {name} is in {markov_state} state with risk score {prediction.risk_score:.2f}"
        context = json.dumps({
            "name": name,
            "state": container.get("state", ""),
            "health": container.get("health", ""),
            "image": container.get("image", ""),
            "memory_pct": round(prediction.memory_pct, 1),
            "cpu_m": round(prediction.cpu_pct, 1),
            "memory_trend": round(prediction.memory_trend, 2),
            "markov_state": markov_state,
            "ttf_minutes": prediction.ttf_minutes,
            "logs": logs[-1000:] if logs else "",
        })
        pred_data = {
            "risk_score": prediction.risk_score,
            "ttf_minutes": prediction.ttf_minutes,
            "confidence": prediction.confidence,
            "markov_state": markov_state,
            "memory_trend": prediction.memory_trend,
        }
        analysis = _llm.analyze(issue, context, pred_data)
        analysis["container"] = name
        analysis["timestamp"] = _now_iso()
        _history.append(analysis)
        if len(_history) > config.HISTORY_MAX:
            _history.pop(0)
        logger.info("LLM analysis for %s: %s", name, analysis.get("analysis", "N/A"))
    except Exception as e:
        logger.error("LLM analysis failed for %s: %s", name, e)


class ReconcileLoop:
    """Background reconcile loop running in a daemon thread."""

    def __init__(self, interval: int = 60):
        self.interval = interval
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._reconcile_fn: Callable = reconcile

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while self.running:
            start = time.monotonic()
            try:
                self._reconcile_fn()
            except Exception as e:
                logger.error("Reconcile error: %s", e)
            elapsed = time.monotonic() - start
            sleep_time = max(0, self.interval - elapsed)
            slept = 0.0
            while slept < sleep_time and self.running:
                time.sleep(min(0.5, sleep_time - slept))
                slept += 0.5


def _setup_logging() -> None:
    level = logging.INFO
    if config.LOG_VERBOSITY.lower() == "debug":
        level = logging.DEBUG
    elif config.LOG_VERBOSITY.lower() == "warn":
        level = logging.WARNING
    logging.basicConfig(
        level=level,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
    )


def _init_llm() -> Optional[LLMAnalyzer]:
    """Initialize LLM analyzer based on LLM_BACKEND config."""
    backend_str = config.LLM_BACKEND.lower()
    try:
        if backend_str == "ollama":
            backend = LLMBackend.OLLAMA
            url = config.OLLAMA_URL
            model = config.OLLAMA_MODEL
            api_key = None
        elif backend_str == "openai":
            backend = LLMBackend.OPENAI
            url = config.OPENAI_API_URL
            model = config.OPENAI_MODEL
            api_key = config.OPENAI_API_KEY or None
        elif backend_str == "saia":
            backend = LLMBackend.SAIA
            url = config.SAIA_API_URL
            model = config.SAIA_MODEL
            api_key = config.SAIA_API_KEY or None
        elif backend_str == "tud":
            backend = LLMBackend.TUD
            url = config.TUD_API_URL
            model = config.TUD_MODEL
            api_key = config.TUD_API_KEY or None
        else:
            logger.warning("Unknown LLM_BACKEND '%s', defaulting to ollama", backend_str)
            backend = LLMBackend.OLLAMA
            url = config.OLLAMA_URL
            model = config.OLLAMA_MODEL
            api_key = None

        analyzer = LLMAnalyzer(
            backend=backend,
            url=url,
            model=model,
            api_key=api_key,
            timeout=config.OLLAMA_TIMEOUT,
        )
        logger.info("LLM backend: %s, url=%s, model=%s", backend_str, url, model)
        return analyzer
    except Exception as e:
        logger.error("Failed to initialize LLM: %s", e)
        return None


def _start_http_server() -> Any:
    from predictive_agent.server import start_server
    return start_server(
        metrics_port=config.METRICS_PORT,
        health_port=config.HEALTH_PORT,
        state_model=_state_model,
        predictor=_predictor,
        cache=_cache,
        reconcile_callback=reconcile,
        history=_history,
    )


def main() -> None:
    """Main entry point — initialize state, start server, run reconcile loop."""
    global _state_model, _predictor, _state_store, _server, _llm

    _setup_logging()
    logger.info("=== %s v%s (Docker) starting ===", config.OPERATOR_NAME, config.OPERATOR_VERSION)
    logger.info("Watch namespaces: %s", config.WATCH_NAMESPACES)
    logger.info("Reconcile interval: %ds", config.RECONCILE_INTERVAL)
    logger.info("LLM backend: %s", config.LLM_BACKEND)
    logger.info("Prediction enabled: %s", config.PREDICTION_ENABLED)

    # Initialize LLM analyzer
    _llm = _init_llm()

    # Initialize state
    _state_model = StateModel()
    _predictor = Predictor(risk_threshold=config.PREDICTION_RISK_THRESHOLD)
    _state_store = StateStore(
        state_model_file=config.STATE_MODEL_FILE,
        predictions_file=config.PREDICTIONS_FILE,
    )

    # Load persisted state
    try:
        _state_model.markov = _state_store.load_markov()
        logger.info("Loaded Markov chain state from %s", config.STATE_MODEL_FILE)
    except Exception as e:
        logger.warning("Could not load Markov state: %s", e)

    # Start HTTP server
    try:
        _server = _start_http_server()
        logger.info("HTTP server started on %d (metrics) and %d (health)",
                    config.METRICS_PORT, config.HEALTH_PORT)
    except Exception as e:
        logger.error("Failed to start HTTP server: %s", e)

    # Handle signals
    loop = ReconcileLoop(interval=config.RECONCILE_INTERVAL)

    def _shutdown(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        loop.stop()
        if _server:
            _server.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    loop.start()
    logger.info("Reconcile loop started with interval %ds", config.RECONCILE_INTERVAL)

    try:
        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        loop.stop()
        if _server:
            _server.shutdown()


if __name__ == "__main__":
    main()
