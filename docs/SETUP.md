# Setup Guide

Everything this lab needs to run **locally, once**, before you ever touch
Kubernetes. If you already ran `scripts/deploy.sh` successfully, your
environment already satisfies all of this — treat this page as the
reference for *why* each tool is here and how to confirm it's healthy.

## What you need, and why

| Tool | Why it's here |
|---|---|
| **Docker Desktop** | Backs minikube's `docker` driver — minikube runs as a container, and the dashboard image is built directly into its engine (no registry needed). |
| **minikube** | The local Kubernetes cluster itself. |
| **kubectl** | Talks to the cluster: apply manifests, exec into pods, stream logs. |
| **Helm** | Installs Cilium and Tetragon — both ship as Helm charts with a lot of moving parts (DaemonSets, CRDs, RBAC); hand-writing that YAML isn't worth it. |

Nothing else is installed on the host — the `hubble` CLI (used to stream
network flows) is built **into the dashboard's own container image**, not
onto your machine. See [`dashboard/Dockerfile`](../dashboard/Dockerfile).

## 1. Verify the tools are present

```bash
docker --version
minikube version
kubectl version --client
helm version
```

All four should print a version with no errors. If any is missing, install
it before continuing — this lab doesn't attempt to install these for you.

## 2. Start Docker Desktop

minikube's `docker` driver needs the Docker Engine running. On Windows:

```bash
"/c/Program Files/Docker/Docker/Docker Desktop.exe" &
```

Then wait for it to actually be ready (the tray icon settling isn't
enough — the engine takes a few seconds after that):

```bash
until docker info >/dev/null 2>&1; do sleep 2; done
docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes RAM'
```

You want to see a real CPU/RAM count, not `0 CPUs, 0 bytes` (that means the
engine isn't up yet — the classic error is
`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`).

## 3. Size the cluster to your machine

`scripts/deploy.sh` starts minikube with:

```bash
minikube start --driver=docker --cni=false --cpus=4 --memory=6144 --disk-size=30g
```

Two flags matter beyond the obvious:

- **`--cni=false`** — minikube normally installs a default CNI (kindnet).
  We disable it because **Cilium replaces the CNI entirely** — installing
  both would fight over pod networking.
- **`--cpus=4 --memory=6144`** — sized to comfortably run Cilium + Envoy +
  Tetragon + Hubble Relay/UI + the dashboard + two sample-app pods on top of
  the Kubernetes control plane, while leaving headroom on an 8 CPU / 10 GB
  Docker Desktop allocation. If your machine has less to give Docker
  Desktop, lower these — Cilium alone needs roughly 2 CPU / 2 GB to stay
  responsive; go much lower and the agent pods will get slow to schedule
  and probes will start flapping.

Check what Docker Desktop is currently allowed to use before committing to
a size:

```bash
docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes RAM'
```

## 4. What "ready" looks like

Once `scripts/deploy.sh` finishes (or you've followed
[DEPLOYMENT.md](DEPLOYMENT.md) by hand), this should be true:

```bash
minikube status
# host: Running, kubelet: Running, apiserver: Running

kubectl get nodes
# one node, STATUS Ready
```

If `kubectl get nodes` shows `NotReady`, the cluster is still converging —
give it another minute before moving on to deployment.

## Next

- [DEPLOYMENT.md](DEPLOYMENT.md) — install Cilium, Tetragon, and the lab itself, step by step, with verification and troubleshooting for each stage.
- [UNDERSTANDING.md](UNDERSTANDING.md) — what's actually happening under the hood, and how to read the dashboard once it's up.
