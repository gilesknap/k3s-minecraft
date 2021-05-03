#!/bin/bash
# source this script for some helper functions to control minecraft on k3s

if [  "$0" == "${BASH_SOURCE[0]}" ]
then
  echo "this script must be sourced to work"
  exit 1
fi

if [[ ! -z "${1}" ]]
then
  MCPASSWD="${1}"
fi
if [ -z ${MCPASSWD} ]
then
  echo "please supply an rcon password as the first parameter or set MCPASSWD"
  exit 1
fi

if [ -z "${MCBACKUP}" ]
then
  echo "please export MCBACKUP=<your mc backup folder>"
  exit 1
fi

ns="minecraft"

export HELM_EXPERIMENTAL_OCI=1
source <(helm completion bash)
source <(kubectl completion bash)

function mclist()
{
    kubectl get deployment -n ${ns} -o "custom-columns=MC SERVER NAME:metadata.annotations.meta\.helm\.sh/release-name,RUNNING:status.replicas"
}

function mccheckname()
{
    export pod=""
    export shortname="${1}"
    export deployname="${1}"-minecraft
    deploy=$(kubectl get deploy -n ${ns} -l app="${deployname}" -o name)

    if [ -z "${deploy}"  ]; then
        echo "please supply one of the following minecraft server names "
        mclist
        return
    fi

    export pod=$(kubectl get pods -l app=${deployname} -o name)
}

function mcstart()
{
    mccheckname "${1}"

    if [ -z ${pod} ]; then
        # the server is not running, spin it up
        export was_shutdown=true

        kubectl scale -n ${ns} ${deploy} --replicas=1

        echo "starting ${deploy} ..."
        while [[ $(kubectl get -n ${ns} pods -l app="${deployname}" -o 'jsonpath={..status.conditions[?(@.type=="Ready")].status}') != "True" ]]
        do
            sleep 1
        done

        export pod=$(kubectl get pods -l app=${deployname} -o name)

        echo "waiting for minecraft server ..."
        while [[ "$(kubectl exec -n ${ns} ${pod} -- /health.sh 2>/dev/null)" != *"motd"* ]]
        do
            sleep 1
        done
    else
        export was_shutdown=false
    fi
}

function mcbackup()
{
    mcstart "${1}"

    if [[ ! -z ${pod} ]]
    then
        tarname=$(date +%Y-%m-%d-%X)-${shortname}.tz

        kubectl exec -n ${ns} ${pod} -- rcon-cli save-off
        kubectl exec -n ${ns} ${pod} -- rcon-cli save-all
        kubectl exec -n ${ns} ${pod} -- tar -czv --exclude='data/logs' /data > ${MCBACKUP}/${tarname}
        kubectl exec -n ${ns} ${pod} -- rcon-cli save-on

        if [ "${was_shutdown}" == "true" ]; then
            echo "stopping ${deploy} ..."
            kubectl scale -n ${ns} ${deploy} --replicas=0
        fi
    fi
}

function mcstop()
{
    mccheckname "${1}"

    if [[ ! -z ${deploy} ]]
    then
        kubectl scale --replicas=0 ${deploy}
    fi
}

function mclog()
{
    mccheckname "${1}"

    if [[ ! -z ${deploy} ]]
    then
        kubectl logs ${deploy}
    fi
}

function mcdeploy()
{
    filename="${1}"
    base=$(basename ${filename})
    releasename="${base%.*}"
    helm repo add minecraft-server-charts https://itzg.github.io/minecraft-server-charts/
    helm upgrade --install ${releasename} -f ${filename} --set minecraftServer.eula=true,rcon.password="${MCPASSWD}" minecraft-server-charts/minecraft
}