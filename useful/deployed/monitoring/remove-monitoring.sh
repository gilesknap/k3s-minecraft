#!/bin/bash

helm delete grafana -n monitoring
helm delete prometheus -n monitoring
kubectl delete -f monitoring.yaml

