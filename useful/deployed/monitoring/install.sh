#!/bin/bash

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add stable https://charts.helm.sh/stable
helm repo update

kubectl create namespace monitoring

echo helm installing kube-prometheus-stack ...
helm upgrade -i prometheus -n monitoring prometheus-community/kube-prometheus-stack -f prometheus-values.yaml

echo port forwarding - please browse to http://localhost:3000 ...
echo def pass is prom-operator
kubectl port-forward -n monitoring deployment/prometheus-grafana 3000
