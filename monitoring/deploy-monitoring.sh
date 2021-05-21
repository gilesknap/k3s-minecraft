#!/bin/bash

kubectl apply -f monitoring.yaml
kubectl config set-context monitoring --namespace monitoring --cluster default --user default
kubectl config use-context monitoring

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts

helm upgrade -i grafana grafana/grafana \
  --set persistence.storageClassName=nfs-storageclass
helm upgrade -i prometheus prometheus-community/prometheus \
  --set alertmanager.persistentVolume.storageClassName=nfs-storageclass \
  --set server.persistentVolume.storageClass=nfs-storageclass \
  --set pushgateway.persistentVolume.storageClass=nfs-storageclass