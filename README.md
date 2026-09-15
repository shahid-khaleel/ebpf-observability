# eBPF Kernel Observability Lab

A local minikube stack that watches a sample application entirely from the
Linux kernel via eBPF - **no agent, sidecar, or code change inside the
monitored application**. It shows:

- **Network connections** - via [Cilium](https://cilium.io) + [Hubble](https://github.com/cilium/hubble) (Hubble UI included)
- **Process creation** - via [Tetragon](https://tetragon.io)
- **File access** - via Tetragon (`fd_install` kprobe)
- **System calls** - via Tetragon (`setuid`, `chmod`, `unlink`/`unlinkat` kprobes)

...plus a threat-intel pipeline matching the architecture below: every
outbound connection's destination IP is checked against a threat-intel feed,
and a match raises an alert.

```
Pod -> Outbound Connection -> Linux Kernel -> eBPF Program -> Security
Monitoring Tool -> Threat Intelligence Feed -> Is IP Malicious? -> Alert / Ignore
```

A single web dashboard (`ebpf-dashboard`) brings all of this together:
connections, alerts, process creation, file access, and syscalls, live.

On top of that, [Kyverno](https://kyverno.io) enforces RBAC hygiene as an
**admission controller** — the opposite point in the pipeline from eBPF's
after-the-fact observation, gatekeeping objects *before* they're ever
created:

```
kubectl apply -> Kubernetes API Server -> Kyverno -> RBAC Policy Check -> PASS -> Object Created
                                                                        -> FAIL -> Request Rejected
```

## Documentation

| Guide | Use it to... |
|---|---|
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | See the whole system in one diagram — the observability path (Cilium/Hubble + Tetragon → dashboard) and the admission-control path (Kyverno) together, with a component reference table. |
| **[docs/SETUP.md](docs/SETUP.md)** | Get the local environment (Docker Desktop, minikube, kubectl, Helm) ready and understand the cluster sizing choices. |
| **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** | Deploy stage by stage with verification commands, plus every real gotcha hit while building this (crash-loops, silent event drops, stale file handles) and the fix for each. |
| **[docs/UNDERSTANDING.md](docs/UNDERSTANDING.md)** | See how the pieces fit together (with a data-flow diagram), how the threat-intel alert pipeline works end to end, how to read each panel, and how to verify it's really live. |
| **[docs/ADMISSION-CONTROL.md](docs/ADMISSION-CONTROL.md)** | How the Kyverno RBAC admission-control layer works (`kubectl apply -> API Server -> Kyverno -> RBAC check -> PASS/FAIL`), both policies explained, a testing walkthrough, and five real Kyverno gotchas hit building it. |
| **[docs/GRPC.md](docs/GRPC.md)** | A standalone gRPC reference, beginner to advanced — protobuf, HTTP/2 internals, the four RPC shapes, security, load balancing — with Hubble Relay (the one real gRPC service in this lab) as the worked example. |

This README is the quickstart; the guides above are the deep dive.

## Architecture

| Component | Role |
|---|---|
| **Cilium** | CNI + eBPF datapath. Every pod's network traffic is observed here, unmodified. |
| **Hubble** (relay + UI) | Streams Cilium's flow data; Hubble UI is Cilium's own network-flow visualizer. |
| **Tetragon** | eBPF-based process/file/syscall visibility (Cilium's sister project). |
| **ebpf-dashboard** | Custom FastAPI app: tails Hubble flows + Tetragon's JSON export, matches destination IPs against `threat-intel/malicious_ips.txt`, and serves one combined UI. |
| **web-backend** | Stock, unmodified `nginx` - the "sample application". |
| **traffic-generator** | Stock `curl` image looping ordinary commands (curl, cat, ls, chmod, rm) to generate the four event classes. Also unmodified/uninstrumented - just an ordinary workload. |
| **Kyverno** | Validating admission controller enforcing two cluster-wide RBAC policies (no default ServiceAccounts, no wildcard Roles/ClusterRoles). |

Everything runs in the `ebpf-lab` namespace except Cilium/Hubble/Tetragon,
which are cluster-wide DaemonSets in `kube-system`.

## Why the "malicious" IPs are safe

`threat-intel/malicious_ips.txt` lists a handful of **RFC 5737 documentation
IPs** (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`) - reserved by IANA
for examples, never routed on the real internet. The traffic-generator
"attacks" these on purpose so the Alerts panel lights up deterministically,
without depending on internet access or flagging any real host. Point
`THREAT_FEED_URL` (env var on the `ebpf-dashboard` Deployment) at a live feed
such as Feodo Tracker to layer in real indicators.

## Deploy

Prerequisites: Docker Desktop, minikube, kubectl, helm (all already
installed/verified on this machine).

```bash
scripts/deploy.sh
```

This is idempotent - safe to re-run after any change. It: starts minikube
(docker driver, no default CNI), installs Cilium+Hubble, installs Tetragon
and its TracingPolicies, installs Kyverno and its RBAC ClusterPolicies,
builds the dashboard image straight into minikube's docker daemon, and
deploys the namespace/configmap/dashboard/sample-app.

## View it

```bash
scripts/port-forward.sh
```

- Dashboard: http://127.0.0.1:8080
- Hubble UI: http://127.0.0.1:8081 (pick namespace `ebpf-lab` to see the sample app's flows as a graph)

Leave the script running; Ctrl+C stops both port-forwards.

## What you'll see

- **Alerts** panel turns red within seconds - `traffic-generator` periodically
  connects to `192.0.2.10` and `198.51.100.23`, both on the threat list.
- **Network Connections** shows every pod-to-pod and pod-to-external flow,
  with direction (EGRESS/INGRESS) and verdict, straight from Cilium's
  datapath.
- **Process Creation** shows every `execve` in the `ebpf-lab` namespace -
  `curl`, `cat`, `ls`, `chmod`, `id`, `whoami`, etc, with full arguments.
- **File Access** shows files opened by those processes (`/etc/passwd`,
  `/tmp/demo-file.txt`, ...).
- **System Calls** shows `setuid`/`chmod`/`unlink` calls made by the sample
  workload.

## Test the RBAC admission control

```bash
cd k8s/kyverno/test-manifests
kubectl apply -f fail-pod-default-sa.yaml        # rejected
kubectl apply -f pass-pod-custom-sa.yaml         # created
kubectl delete -f pass-pod-custom-sa.yaml        # clean up
```

Full walkthrough (with the exact rejection output and all five gotchas hit
building this) in
[docs/ADMISSION-CONTROL.md](docs/ADMISSION-CONTROL.md).

## Notes / known limitations

- **In-memory only**: the dashboard keeps the last 300 events per category in
  memory. A pod restart clears history (by design - this is a live tail, not
  a SIEM).
- **Single-node hostPath**: the dashboard reads Tetragon's JSON export via a
  `hostPath` mount, which only works because minikube here is single-node.
- **PID-based correlation with retry**: Tetragon's file/syscall events arrive
  without pod info in this environment and get backfilled by the dashboard;
  see [docs/UNDERSTANDING.md](docs/UNDERSTANDING.md) for how and why.

Full detail on all of the above, plus every gotcha hit while building this
and its fix, is in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Repo layout

```
docs/
  ARCHITECTURE.md                 the whole system in one diagram
  SETUP.md                        environment prerequisites & cluster sizing
  DEPLOYMENT.md                   stage-by-stage deploy + troubleshooting
  UNDERSTANDING.md                architecture, data flow, panel guide, testing
  ADMISSION-CONTROL.md            how the Kyverno RBAC admission layer works
  GRPC.md                         gRPC reference, beginner to advanced
k8s/
  namespace.yaml                 ebpf-lab namespace
  tetragon-values.yaml           Helm values for Tetragon
  tetragon-policies/             TracingPolicy CRDs (file + syscall monitoring)
  kyverno/
    policies/                    ClusterPolicy CRDs (RBAC admission control)
    test-manifests/              ready-made PASS/FAIL manifests for testing them
  dashboard.yaml                 ebpf-dashboard Deployment + Service
  sample-app/
    web-backend.yaml             stock nginx "application" (dedicated ServiceAccount)
    traffic-generator.yaml       stock curl image generating activity (dedicated ServiceAccount)
dashboard/                       FastAPI app + static UI + Dockerfile
threat-intel/
  malicious_ips.txt              demo threat-intel feed (RFC 5737 IPs)
scripts/
  deploy.sh                      one-shot end-to-end deploy
  port-forward.sh                open the dashboard + Hubble UI
```
