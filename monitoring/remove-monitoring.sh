#!/bin/bash

helm delete grafana
helm delete prometheus
kubectl delete -f monitoring.yaml

