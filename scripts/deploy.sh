#!/usr/bin/env bash
# End-to-end deploy for the eBPF kernel observability lab on minikube.
# Safe to re-run; every step is idempotent (helm upgrade --install / kubectl apply).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> Starting minikube (docker driver, no default CNI - Cilium replaces it)"
minikube status >/dev/null 2>&1 || minikube start --driver=docker --cni=false --cpus=4 --memory=6144 --disk-size=30g

echo "==> Adding Cilium helm repo"
helm repo add cilium https://helm.cilium.io/ >/dev/null
helm repo update cilium >/dev/null

echo "==> Installing Cilium (CNI + Hubble + Hubble UI)"
helm upgrade --install cilium cilium/cilium --version 1.19.6 --namespace kube-system \
  --set hubble.relay.enabled=true \
  --set hubble.ui.enabled=true \
  --set hubble.ui.service.type=NodePort \
  --set ipam.mode=kubernetes \
  --set kubeProxyReplacement=false \
  --set operator.replicas=1

kubectl -n kube-system rollout status daemonset/cilium --timeout=180s
kubectl -n kube-system rollout status deployment/cilium-operator --timeout=120s
kubectl -n kube-system rollout status deployment/hubble-relay --timeout=120s
kubectl -n kube-system rollout status deployment/hubble-ui --timeout=120s

echo "==> Installing Tetragon (process / file / syscall visibility)"
helm upgrade --install tetragon cilium/tetragon --version 1.7.0 --namespace kube-system \
  --set tetragon.enableProcessCred=true \
  --set tetragon.enableProcessNs=true \
  -f k8s/tetragon-values.yaml
kubectl -n kube-system rollout status daemonset/tetragon --timeout=120s

echo "==> Applying Tetragon TracingPolicies (file access + syscalls)"
kubectl apply -f k8s/tetragon-policies/file-monitoring.yaml
kubectl apply -f k8s/tetragon-policies/syscall-monitoring.yaml

echo "==> Building dashboard image inside minikube's docker daemon"
eval "$(minikube docker-env)"
docker build -t ebpf-lab/dashboard:latest dashboard

echo "==> Deploying namespace, threat-intel feed, dashboard, sample app"
kubectl apply -f k8s/namespace.yaml
kubectl -n ebpf-lab create configmap threat-intel-feed \
  --from-file=malicious_ips.txt=threat-intel/malicious_ips.txt \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/dashboard.yaml
kubectl apply -f k8s/sample-app/web-backend.yaml
kubectl apply -f k8s/sample-app/traffic-generator.yaml

kubectl -n ebpf-lab rollout status deployment/ebpf-dashboard --timeout=120s
kubectl -n ebpf-lab rollout status deployment/web-backend --timeout=120s
kubectl -n ebpf-lab rollout status deployment/traffic-generator --timeout=120s

echo
echo "==> Done. Run scripts/port-forward.sh to open the dashboard and Hubble UI."
