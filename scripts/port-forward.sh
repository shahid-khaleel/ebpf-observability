#!/usr/bin/env bash
# Opens the eBPF dashboard on http://127.0.0.1:8080 and Hubble UI on
# http://127.0.0.1:8081. Leave this running; Ctrl+C stops both.
set -euo pipefail

cleanup() { kill 0; }
trap cleanup EXIT INT TERM

kubectl -n ebpf-lab port-forward svc/ebpf-dashboard 8080:80 &
kubectl -n kube-system port-forward svc/hubble-ui 8081:80 &

echo "eBPF dashboard : http://127.0.0.1:8080"
echo "Hubble UI      : http://127.0.0.1:8081"
echo "Ctrl+C to stop."
wait
