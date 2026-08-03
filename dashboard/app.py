import base64
import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

MAXLEN = 300

connections = deque(maxlen=MAXLEN)
alerts = deque(maxlen=MAXLEN)
processes = deque(maxlen=MAXLEN)
file_events = deque(maxlen=MAXLEN)
syscall_events = deque(maxlen=MAXLEN)
events_lock = threading.Lock()

# process_kprobe events (file access, syscalls) come back from Tetragon
# without pod/namespace/binary attached in this environment (only exec/exit
# events carry full process context here). We backfill kprobe events by
# correlating on PID against the most recent exec event for that PID.
PID_CACHE_MAX = 4096
pid_to_proc = {}
pid_cache_lock = threading.Lock()


def remember_pid(pid, pod, namespace, binary):
    if pid is None:
        return
    with pid_cache_lock:
        if len(pid_to_proc) >= PID_CACHE_MAX:
            pid_to_proc.pop(next(iter(pid_to_proc)))
        pid_to_proc[pid] = {"pod": pod, "namespace": namespace, "binary": binary}


def lookup_pid(pid):
    with pid_cache_lock:
        return pid_to_proc.get(pid)


# Tetragon's exporter can flush a process_kprobe line to the export file
# *before* the process_exec line for the same PID, even though exec always
# happens first in the kernel (exec enrichment - pod/container lookup - is
# slower than the plain kprobe encode). So an immediate PID-cache miss isn't
# final: park the event and keep retrying for a few seconds.
# Tetragon's own process-cache enrichment can retry for up to
# event-cache-retries * event-cache-retry-delay (default 15 * 2s = 30s)
# before an exec event's pod metadata is fully resolved and written out, so
# the retry window here needs to comfortably exceed that.
PENDING_TTL_SECONDS = 45
pending_kprobes = deque()
pending_lock = threading.Lock()
resolve_stats = {"resolved": 0, "expired": 0}


def resolve_pending_loop():
    while True:
        time.sleep(0.5)
        now = time.time()
        with pending_lock:
            items, remaining = list(pending_kprobes), deque()
            pending_kprobes.clear()
        for item in items:
            cached = lookup_pid(item["pid"])
            if cached:
                _finalize_kprobe(item, cached["pod"], cached["namespace"], cached["binary"])
                resolve_stats["resolved"] += 1
            elif now - item["queued_at"] < PENDING_TTL_SECONDS:
                remaining.append(item)
            else:
                resolve_stats["expired"] += 1
                # too old, drop (unattributed host/control-plane noise)
        if remaining:
            with pending_lock:
                pending_kprobes.extend(remaining)


def _finalize_kprobe(item, pod_name, namespace, binary):
    if item["kind"] == "file":
        record = {
            "time": item["time"], "binary": binary, "path": item["path"],
            "pod": pod_name, "namespace": namespace, "pid": item["pid"],
        }
        with events_lock:
            file_events.appendleft(record)
    else:
        record = {
            "time": item["time"], "syscall": item["syscall"], "binary": binary,
            "pod": pod_name, "namespace": namespace, "pid": item["pid"],
        }
        with events_lock:
            syscall_events.appendleft(record)

THREAT_FILE = os.environ.get("THREAT_INTEL_FILE", "/etc/threat-intel/malicious_ips.txt")
THREAT_FEED_URL = os.environ.get("THREAT_FEED_URL", "")
HUBBLE_RELAY_ADDR = os.environ.get("HUBBLE_RELAY_ADDR", "hubble-relay.kube-system.svc.cluster.local:80")
TETRAGON_LOG_PATH = os.environ.get("TETRAGON_LOG_PATH", "/var/run/cilium/tetragon/tetragon.log")

malicious_ips = set()
malicious_lock = threading.Lock()


def load_threat_intel():
    while True:
        ips = set()
        try:
            with open(THREAT_FILE) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        ips.add(line)
        except FileNotFoundError:
            pass
        if THREAT_FEED_URL:
            try:
                resp = requests.get(THREAT_FEED_URL, timeout=5)
                for line in resp.text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        ips.add(line)
            except Exception as e:
                print(f"[threat-intel] failed to fetch {THREAT_FEED_URL}: {e}")
        with malicious_lock:
            malicious_ips.clear()
            malicious_ips.update(ips)
        print(f"[threat-intel] loaded {len(ips)} indicators")
        time.sleep(60)


def is_malicious(ip):
    if not ip:
        return False
    with malicious_lock:
        return ip in malicious_ips


def _get(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict) and k in d:
            return d[k]
    return default


def handle_hubble_line(line):
    line = line.strip()
    if not line:
        return
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return
    flow = evt.get("flow")
    if not flow:
        return

    ip = flow.get("IP", {}) or {}
    l4 = flow.get("l4", {}) or {}
    src_ep = flow.get("source", {}) or {}
    dst_ep = flow.get("destination", {}) or {}

    proto, dport, sport = "OTHER", None, None
    if "TCP" in l4:
        proto = "TCP"
        dport = _get(l4["TCP"], "destination_port", "DestinationPort")
        sport = _get(l4["TCP"], "source_port", "SourcePort")
    elif "UDP" in l4:
        proto = "UDP"
        dport = _get(l4["UDP"], "destination_port", "DestinationPort")
        sport = _get(l4["UDP"], "source_port", "SourcePort")

    dst_ip = ip.get("destination")
    record = {
        "time": flow.get("time"),
        "src_ip": ip.get("source"),
        "src_pod": src_ep.get("pod_name"),
        "src_ns": src_ep.get("namespace"),
        "dst_ip": dst_ip,
        "dst_pod": dst_ep.get("pod_name"),
        "dst_ns": dst_ep.get("namespace"),
        "src_port": sport,
        "dst_port": dport,
        "protocol": proto,
        "verdict": flow.get("verdict"),
        "direction": flow.get("traffic_direction"),
        "summary": flow.get("Summary") or flow.get("summary"),
        "malicious": is_malicious(dst_ip),
    }
    with events_lock:
        connections.appendleft(record)
        if record["malicious"]:
            alert = dict(record)
            alert["reason"] = "destination IP matches threat-intel feed"
            alerts.appendleft(alert)


def tail_hubble():
    while True:
        try:
            proc = subprocess.Popen(
                [
                    "hubble", "observe",
                    "--server", HUBBLE_RELAY_ADDR,
                    "-o", "json",
                    "--follow",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            for line in proc.stdout:
                handle_hubble_line(line)
            err = proc.stderr.read()
            print(f"[hubble] observe process exited, stderr: {err}")
        except Exception as e:
            print(f"[hubble] tail error: {e}")
        time.sleep(5)


def _file_arg_path(pk):
    for a in pk.get("args", []):
        fa = a.get("file_arg")
        if fa:
            return fa.get("path")
    return None


def handle_tetragon_line(line):
    line = line.strip()
    if not line:
        return
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return

    if "process_exec" in evt:
        p = evt["process_exec"].get("process", {})
        pod = p.get("pod") or {}
        record = {
            "time": evt.get("time"),
            "event": "exec",
            "binary": p.get("binary"),
            "arguments": p.get("arguments", ""),
            "pod": pod.get("name"),
            "namespace": pod.get("namespace"),
            "pid": p.get("pid"),
        }
        if pod.get("name"):
            remember_pid(p.get("pid"), pod.get("name"), pod.get("namespace"), p.get("binary"))
        with events_lock:
            processes.appendleft(record)

    elif "process_exit" in evt:
        p = evt["process_exit"].get("process", {})
        pod = p.get("pod") or {}
        record = {
            "time": evt.get("time"),
            "event": "exit",
            "binary": p.get("binary"),
            "pod": pod.get("name"),
            "namespace": pod.get("namespace"),
            "pid": p.get("pid"),
        }
        with events_lock:
            processes.appendleft(record)

    elif "process_kprobe" in evt:
        pk = evt["process_kprobe"]
        p = pk.get("process", {}) or {}
        pod = p.get("pod") or {}
        func = pk.get("function_name", "")
        pid = p.get("pid")

        pod_name, namespace, binary = pod.get("name"), pod.get("namespace"), p.get("binary")
        if not pod_name:
            cached = lookup_pid(pid)
            if cached:
                pod_name, namespace, binary = cached["pod"], cached["namespace"], cached["binary"]

        kind = "file" if func == "fd_install" else "syscall"
        item = {
            "kind": kind, "pid": pid, "time": evt.get("time"),
            "path": _file_arg_path(pk) if kind == "file" else None,
            "syscall": func,
        }

        if pod_name:
            _finalize_kprobe(item, pod_name, namespace, binary)
        else:
            # No cached pod for this PID yet - exec enrichment for it may
            # still be in flight. Queue for a short retry window instead of
            # dropping outright (see resolve_pending_loop).
            item["queued_at"] = time.time()
            with pending_lock:
                pending_kprobes.append(item)


def tail_tetragon():
    path = Path(TETRAGON_LOG_PATH)
    while not path.exists():
        time.sleep(1)
    with path.open("r") as f:
        f.seek(0, os.SEEK_END)
        while True:
            line = f.readline()
            if not line:
                time.sleep(0.3)
                continue
            handle_tetragon_line(line)


app = FastAPI()
STATIC_DIR = Path(__file__).parent / "static"


@app.on_event("startup")
def start_background_threads():
    threading.Thread(target=load_threat_intel, daemon=True, name="threat-intel").start()
    threading.Thread(target=tail_hubble, daemon=True, name="hubble").start()
    threading.Thread(target=tail_tetragon, daemon=True, name="tetragon").start()
    threading.Thread(target=resolve_pending_loop, daemon=True, name="resolver").start()


@app.get("/api/threads")
def api_threads():
    return [{"name": t.name, "alive": t.is_alive()} for t in threading.enumerate()]


@app.get("/api/checkpid/{pid}")
def api_checkpid(pid: int):
    return {"pid": pid, "cached": lookup_pid(pid)}


@app.get("/api/connections")
def api_connections():
    with events_lock:
        return list(connections)


@app.get("/api/alerts")
def api_alerts():
    with events_lock:
        return list(alerts)


@app.get("/api/processes")
def api_processes():
    with events_lock:
        return list(processes)


@app.get("/api/files")
def api_files():
    with events_lock:
        return list(file_events)


@app.get("/api/syscalls")
def api_syscalls():
    with events_lock:
        return list(syscall_events)


@app.get("/api/threat-intel")
def api_threat_intel():
    with malicious_lock:
        return sorted(malicious_ips)


@app.get("/api/debug")
def api_debug():
    with pid_cache_lock:
        pid_cache_size = len(pid_to_proc)
        sample_pids = list(pid_to_proc.keys())[-5:]
    with pending_lock:
        pending_size = len(pending_kprobes)
        pending_sample = [{"pid": i["pid"], "kind": i["kind"], "age": round(time.time() - i["queued_at"], 2)} for i in list(pending_kprobes)[:5]]
    return {
        "pid_cache_size": pid_cache_size,
        "sample_cached_pids": sample_pids,
        "pending_size": pending_size,
        "pending_sample": pending_sample,
        "resolve_stats": resolve_stats,
    }


@app.get("/api/stats")
def api_stats():
    with events_lock:
        return {
            "connections": len(connections),
            "alerts": len(alerts),
            "processes": len(processes),
            "files": len(file_events),
            "syscalls": len(syscall_events),
        }


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC_DIR / "index.html").read_text()
