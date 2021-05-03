#!/bin/bash
# source this script for some helper functions to control minecraft on k3s

if [  "$0" == "${BASH_SOURCE[0]}" ]
then
  echo "this script must be sourced to work, e.g.:"
  echo "  source mc-k8s.sh"
  exit 1
fi

export HELM_EXPERIMENTAL_OCI=1
source <(helm completion bash)
source <(kubectl completion bash)

function mclist()
{
    kubectl get deployment -n minecraft -o "custom-columns=MC SERVER NAME:metadata.annotations.meta\.helm\.sh/release-name,RUNNING:status.replicas"
}

function mccheckname()
{
    export pod=""
    export shortname="${1}"
    export deployname="${1}"-minecraft
    deploy=$(kubectl get deploy -n minecraft -l app="${deployname}" -o name)

    if [ -z "${deploy}"  ]; then
        echo "please supply one of the following minecraft server names "
        mclist
        return
    fi

    export pod=$(kubectl get pods -l app=${deployname} -o name)
}

function mccheck ()
{
    mccheckname "${1}"

    if [ ${pod} ]; then
        output=$(kubectl exec -n minecraft ${pod} -- /health.sh)
        echo "${output}"
        # a good result contains a motd
        if [[ "${output}" == *"motd"* ]]; then
          return 0
        fi
    fi
    return 1
}


function mcstart()
{
    mccheckname "${1}"

    if [ -z ${pod} ]; then
        # the server is not running, spin it up
        export was_shutdown=true

        kubectl scale -n minecraft ${deploy} --replicas=1

        echo "starting ${deploy} ..."
        while [[ $(kubectl get -n minecraft pods -l app="${deployname}" -o 'jsonpath={..status.conditions[?(@.type=="Ready")].status}') != "True" ]]
        do
            sleep 1
        done

        export pod=$(kubectl get pods -l app=${deployname} -o name)

        echo "waiting for minecraft server ..."
        while ! mccheck ${1}
        do
            sleep 1
        done
    else
        echo ${1} is running
        export was_shutdown=false
    fi
}

function mcbackup()
{
    MCBACKUP=${MCBACKUP:-$(read -p "path to backup folder: " IN; echo $IN)}

    mcstart "${1}"

    if [[ ${pod} ]]
    then
        tarname=$(date +%Y-%m-%d-%X)-${shortname}.tz

        kubectl exec -n minecraft ${pod} -- rcon-cli save-off
        kubectl exec -n minecraft ${pod} -- rcon-cli save-all
        kubectl exec -n minecraft ${pod} -- tar -czv --exclude='data/logs' /data > ${MCBACKUP}/${tarname}
        kubectl exec -n minecraft ${pod} -- rcon-cli save-on

        if [ "${was_shutdown}" == "true" ]; then
            echo "stopping ${deploy} ..."
            kubectl scale -n minecraft ${deploy} --replicas=0
        fi
    fi
}

function mcstop()
{
    mccheckname "${1}"

    if [[ ${deploy} ]]
    then
        if ! mccheck ${1}; then
          echo "${1} is not running"
        else
          kubectl scale --replicas=0 ${deploy}
        fi
    fi
}

function mclog()
{
    mccheckname "${1}"

    if [[ ${deploy} ]]
    then
        kubectl logs ${deploy}
    fi
}

function mcdeploy()
{
    filename="${1}"
    base=$(basename ${filename})
    releasename="${base%.*}"
    MCPASSWD=${MCPASSWD:-$(read -p "password for rcon: " IN; echo $IN)}
    MCEULA=${MCEULA:-$(read -p "agree to minecraft EULA? (type 'true'): " IN; echo $IN)}
    helm repo add minecraft-server-charts https://itzg.github.io/minecraft-server-charts/
    helm upgrade --install ${releasename} -f ${filename} --set minecraftServer.eula=${MCEULA},rcon.password="${MCPASSWD}" minecraft-server-charts/minecraft
}