#!/bin/bash

# run this at startup on the machine where you run your browser
# It will proxy connections to web apps in the cluster

# kubernetes dashboard
kubectl proxy &

# grafana
export POD_NAME=$(kubectl get pods -n monitoring -l "app.kubernetes.io/name=grafana,app.kubernetes.io/instance=grafana" -o jsonpath="{.items[0].metadata.name}")
kubectl --namespace monitoring port-forward $POD_NAME 3000 &

# the remaining prometheus server port forward should be unecessary if I could
# work out how to do server side authentication - the clue should be in this
# secret but I have yet to crack it
#  ---- kubectl get secret -n monitoring prometheus-server-token-mqftd -o

# prometheus server
export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=server" -o jsonpath="{.items[0].metadata.name}")
kubectl --namespace monitoring port-forward $POD_NAME 9090 &
# # prometheus alert manager
# export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=alertmanager" -o jsonpath="{.items[0].metadata.name}")
# kubectl --namespace monitoring port-forward $POD_NAME 9093
# # prometheus push gateway
# export POD_NAME=$(kubectl get pods --namespace monitoring -l "app=prometheus,component=pushgateway" -o jsonpath="{.items[0].metadata.name}")
#   kubectl --namespace monitoring port-forward $POD_NAME 9091 &

# for more exporters see the charts in https://github.com/prometheus-community/helm-charts/tree/main/charts
# or do 'helm search repo prometheus-community'

