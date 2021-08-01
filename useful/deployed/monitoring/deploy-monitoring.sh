#!/bin/bash

kubectl apply -f monitoring.yaml
kubectl config set-context monitoring --namespace monitoring --cluster default --user default
kubectl config use-context monitoring

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts

helm upgrade -i grafana grafana/grafana \
  -f grafana.values.yaml \
  --namespace monitoring
helm upgrade -i prometheus prometheus-community/prometheus \
  -f prometheus.values.yaml \
  --namespace monitoring

# to use grafana:
# create new data source - prometheus with
#  Url: http://localhost:9090
#  Access: Browser
# you also need the 9090 port-forward from gui-proxies.sh
# then import a dashboard and choose id 1860 (node exporter full)
# this gives extensive stats on all nodes in the cluster