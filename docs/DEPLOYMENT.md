# Deployment Guide

A step-by-step walkthrough of `scripts/deploy.sh` — what each stage does,
how to verify it before moving to the next one, and how to recover if it
doesn't come up clean. Everything here is idempotent; re-running any stage
(or the whole script) is safe.

If you just want it deployed, run:

```bash
scripts/deploy.sh
```

If you want to understand or debug each stage, read on.

---

## Stage 1 — Cluster

```bash
minikube start --driver=docker --cni=false --cpus=4 --memory=6144 --disk-size=30g
```

**Verify:**
```bash
kubectl get nodes
# NAME       STATUS   ROLES           AGE   VERSION
# minikube   Ready    control-plane   ...
```

At this point pods **without** `hostNetwork: true` won't get IPs yet —
there's no CNI installed on purpose (see [SETUP.md](SETUP.md)). That's
expected; Cilium fixes it in the next stage.

---

## Stage 2 — Cilium (CNI + Hubble)

```bash
helm repo add cilium https://helm.cilium.io/
helm repo update cilium

helm upgrade --install cilium cilium/cilium --version 1.19.6 --namespace kube-system \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set hubble.ui.service.type=NodePort \
  --set ipam.mode=kubernetes \
  --set kubeProxyReplacement=false \
  --set operator.replicas=1
```

Notes on the values:
- `kubeProxyReplacement=false` — minikube already installs `kube-proxy`;
  we let Cilium act as a normal CNI alongside it rather than replacing it
  (full kube-proxy replacement is a bigger, riskier change than this lab
  needs).
- `hubble.relay.enabled` / `hubble.ui.enabled` — without these, Cilium
  still captures flow data internally, but nothing exposes it outside the
  agent. Relay aggregates flows cluster-wide; UI visualizes them.

**Verify:**
```bash
kubectl -n kube-system rollout status daemonset/cilium --timeout=180s
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=120s
kubectl -n kube-system rollout status deployment/hubble-relay --timeout=120s
kubectl -n kube-system rollout status deployment/hubble-ui --timeout=120s

kubectl get pods -n kube-system -o wide
# pods should now have real IPs (10.244.x.x), not stuck in ContainerCreating
```

### Known gotcha: hubble-relay / hubble-ui crash-looping on first install

On a freshly-created cluster, `hubble-relay` and `hubble-ui` sometimes get
scheduled and get their pod IPs **before** Cilium's own datapath has fully
converged, and end up stuck restarting (`hubble-relay` failing DNS lookups
to `hubble-peer`, `hubble-ui`'s frontend container failing its liveness
probe with `no route to host`). This is a one-time startup race, not a
config problem — the fix is just to give them a clean restart once Cilium
and CoreDNS are stable:

```bash
kubectl -n kube-system delete pod -l k8s-app=hubble-relay --wait=false
kubectl -n kube-system delete pod -l k8s-app=hubble-ui --wait=false
```

They should come up `1/1` and `2/2` respectively within ~30s.

---

## Stage 3 — Tetragon (process / file / syscall visibility)

```bash
helm upgrade --install tetragon cilium/tetragon --version 1.7.0 --namespace kube-system \
  --set tetragon.enableProcessCred=true \
  --set tetragon.enableProcessNs=true \
  -f k8s/tetragon-values.yaml
```

**Verify:**
```bash
kubectl -n kube-system rollout status daemonset/tetragon --timeout=120s
kubectl -n kube-system get pods -l app.kubernetes.io/name=tetragon
# READY 2/2  (containers: tetragon, export-stdout)
```

### Known gotcha #1: an empty `exportAllowList` blocks *everything*

Tetragon's JSON export is gated by an allowlist
(`tetragon.exportAllowList`). The chart's real default is:

```
{"event_set":["PROCESS_EXEC", "PROCESS_EXIT", "PROCESS_KPROBE", "PROCESS_UPROBE", "PROCESS_TRACEPOINT", "PROCESS_LSM"]}
```

Passing `--set tetragon.exportAllowList=''` (to "clear" it) does **not**
mean "no restriction" — Tetragon reads an empty allowlist as *allow
nothing*, so every event silently vanishes before it ever reaches the
export file, with no error anywhere. If you ever see zero events from
`export-stdout` despite the agent clearly running, check this first:

```bash
kubectl -n kube-system get cm tetragon-config -o jsonpath='{.data.export-allowlist}'
```

If it's empty, `helm upgrade` back to the chart default (just omit the
override) fixes it immediately.

### Known gotcha #2: the default export denylist drops valid workload events

The chart's default `exportDenyList` filters out `health_check` noise plus
**any event with `namespace` in `["", "cilium", "kube-system"]`** — sensible
for cutting control-plane noise. But in this environment, Tetragon's
`process_kprobe` events (the ones backing the File Access and System Calls
panels) come back from the agent **without pod/namespace attached at all**
— only `process_exec`/`process_exit` carry full pod context here. An
unattributed kprobe event gets treated as `namespace: ""` and silently
denied, even when it genuinely belongs to a workload pod in `ebpf-lab`.

This repo's [`k8s/tetragon-values.yaml`](../k8s/tetragon-values.yaml)
relaxes the export denylist to just `{"health_check":true}` — the
dashboard backend does its own pod-attribution and its own filtering
instead (see [UNDERSTANDING.md](UNDERSTANDING.md) for how). If you ever
"lose" this fix via a `helm upgrade` that doesn't pass `-f
k8s/tetragon-values.yaml`, file/syscall events will silently stop counting
up in `/api/stats` even though `tetra tracingpolicy list` shows them firing
in-kernel (`NPOST` counters climbing).

**How to confirm the export pipeline itself is healthy**, independent of
the dashboard:
```bash
kubectl -n kube-system logs ds/tetragon -c export-stdout --tail=5
```

---

## Stage 4 — TracingPolicies (file access + syscalls)

```bash
kubectl apply -f k8s/tetragon-policies/file-monitoring.yaml
kubectl apply -f k8s/tetragon-policies/syscall-monitoring.yaml
```

These are custom resources (`TracingPolicy`), not part of the Helm chart —
Tetragon's *default* sensor only covers process exec/exit. Watching file
opens and specific syscalls requires explicitly telling it which kernel
functions to hook (see [UNDERSTANDING.md](UNDERSTANDING.md) for what each
policy actually does).

**Verify:**
```bash
kubectl get tracingpolicies.cilium.io
kubectl -n kube-system exec ds/tetragon -c tetragon -- tetra tracingpolicy list
# STATE should be "enabled" for both, NPOST counters should be > 0 and climbing
```

A `kprobe spec pre-validation issued a warning` in the agent logs for these
policies is expected and harmless (a minor BTF type-name mismatch on a
couple of arguments); it does not stop the policy from loading or firing.

---

## Stage 5 — Dashboard image

```bash
eval "$(minikube docker-env)"
docker build -t ebpf-lab/dashboard:latest dashboard
```

`eval "$(minikube docker-env)"` points your shell's `docker` CLI at
**minikube's internal Docker daemon** instead of your host's. The image
gets built directly inside the cluster's container runtime — no registry,
no `minikube image load`, no push/pull round-trip. This is why
[`k8s/dashboard.yaml`](../k8s/dashboard.yaml) sets
`imagePullPolicy: Never`: the image already exists locally to the node: pulling
would just fail (or worse, silently try to hit a real registry with your
public tag).

**Verify:**
```bash
eval "$(minikube docker-env)"
docker images | grep ebpf-lab
```

---

## Stage 6 — The lab itself

```bash
kubectl apply -f k8s/namespace.yaml

kubectl -n ebpf-lab create configmap threat-intel-feed \
  --from-file=malicious_ips.txt=threat-intel/malicious_ips.txt \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -f k8s/dashboard.yaml
kubectl apply -f k8s/sample-app/web-backend.yaml
kubectl apply -f k8s/sample-app/traffic-generator.yaml
```

The `create configmap ... --dry-run=client -o yaml | kubectl apply -f -`
pattern re-syncs the ConfigMap from the file on every deploy (a plain
`kubectl create configmap` would fail with "already exists" on a re-run).

**Verify:**
```bash
kubectl -n ebpf-lab rollout status deployment/ebpf-dashboard --timeout=120s
kubectl -n ebpf-lab rollout status deployment/web-backend --timeout=120s
kubectl -n ebpf-lab rollout status deployment/traffic-generator --timeout=120s

kubectl -n ebpf-lab get pods
# all three 1/1 Running
```

### Known gotcha: hostPath needs a fresh dashboard pod after any Tetragon restart

The dashboard reads Tetragon's JSON export via a `hostPath` mount
(`/var/run/cilium/tetragon/tetragon.log`), tailed from the position it was
at when the container started. If the Tetragon DaemonSet pod restarts (e.g.
after a `helm upgrade`), the export file gets recreated — a dashboard pod
that was already running keeps a file handle to the *old, now-deleted*
inode and silently stops seeing new events (no error, no crash — the tail
loop just goes quiet). If you upgrade Tetragon after the dashboard is
already up:

```bash
kubectl -n ebpf-lab rollout restart deployment/ebpf-dashboard
```

---

## Full verification

```bash
scripts/port-forward.sh   # in one terminal, leave it running
```

Then in another terminal:
```bash
curl -s http://127.0.0.1:8080/api/stats
# {"connections":N,"alerts":N,"processes":N,"files":N,"syscalls":N}
```

All five counts should be **non-zero and climbing** if you check twice a
few seconds apart. If any of them are stuck at zero, see the matching
gotcha above, or the troubleshooting steps in
[UNDERSTANDING.md](UNDERSTANDING.md#testing--verification).

## Redeploying after a code change

Only the dashboard has code in this repo (everything else is
config/policy). After editing `dashboard/app.py` or the static UI:

```bash
eval "$(minikube docker-env)"
docker build -t ebpf-lab/dashboard:latest dashboard
kubectl -n ebpf-lab rollout restart deployment/ebpf-dashboard
```

## Tearing down

```bash
helm uninstall tetragon -n kube-system
helm uninstall cilium -n kube-system
kubectl delete namespace ebpf-lab
```

Or to remove everything including the cluster itself:
```bash
minikube delete
```
