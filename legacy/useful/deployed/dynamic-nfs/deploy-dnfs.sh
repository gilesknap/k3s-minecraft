#!/bin/bash

if [ "${1}" == "-u" ]; then
    kubectl delete -n default -f 01-svc-account.yaml
    kubectl delete -n default -f 02-storage-provisioner.yaml
else
    kubectl apply -n default -f 01-svc-account.yaml
    kubectl apply -n default -f 02-storage-provisioner.yaml
fi
