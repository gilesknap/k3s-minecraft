#!/bin/bash

kubectl apply -f monitoring.yaml
kubectl config set-context monitoring --namespace monitoring --cluster default --user default
kubectl config use-context monitoring

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts

# for some reason nfs-storageclass causes grafana pod to exit 1 with no log
# so use local-path and tie to my server gknuc for local storage
helm upgrade -i grafana grafana/grafana \
  --set persistence.storageClassName=local-path \
  --set persistence.enabled=true \
  --set nodeSelector."kubernetes\\.io/hostname"=gknuc
helm upgrade -i prometheus prometheus-community/prometheus \
  -f prometheus.values.yaml \
  --set alertmanager.persistentVolume.storageClass=nfs-storageclass \
  --set server.persistentVolume.storageClass=nfs-storageclass \
  --set pushgateway.persistentVolume.storageClass=nfs-storageclass \
  --set pushgateway.persistentVolume.enabled=true

# to use grafana:
# create new data source - prometheus with
#  Url: http://localhost:9090
#  Access: Browser
# you also need the 9090 port-forward from gui-proxies.sh
# then import a dashboard and choose id 1860 (node exporter full)
# this gives extensive stats on all nodes in the cluster