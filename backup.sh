#!/bin/bash
set -e

ns="minecraft"

deploy=$(kubectl get deploy -n ${ns} -l app="${1}" -o name)

if [ -z "${deploy}"  ]; then
    echo ${deploy}
    echo "please supply one of the following minecraft server names "
    kubectl get deployment -n ${ns} -o name
    exit 1
fi

pod=$(kubectl get pods -l app=${1} -o name)
if [ -z ${pod} ]; then
    # the server is not running, spin it up for the backup only
    kubectl scale -n ${ns} ${deploy} --replicas=1

    echo "starting ${deploy} ..."
    while [[ $(kubectl get -n ${ns} pods -l app="${1}" -o 'jsonpath={..status.conditions[?(@.type=="Ready")].status}') != "True" ]]
    do
        sleep 1
    done

    shutdown=true
    pod=$(kubectl get pods -l app=${1} -o name)

    echo "waiting for minecraft server ..."
    while [[ "$(kubectl exec -n ${ns} ${pod} -- /health.sh 2>/dev/null)" != *"motd"* ]]
    do
        sleep 1
    done
fi


deployment="${1}"
name=$(date +%Y-%m-%d-%X)-deployment.tz
kubectl exec -n ${ns} ${pod} -- rcon-cli save-off
kubectl exec -n ${ns} ${pod} -- rcon-cli save-all
kubectl exec -n ${ns} ${pod} -- tar -czv --exclude='data/logs' /data > /mnt/bigdisk/minecraft-k8s-backup/${name}
kubectl exec -n ${ns} ${pod} -- rcon-cli save-on

if [ "${shutdown}" == "true" ]; then
    echo "stopping ${deploy} ..."
    kubectl scale -n ${ns} ${deploy} --replicas=0
fi
