#!/bin/bash
# source this script for some helper functions to control minecraft on k3s

if [  "$0" == "${BASH_SOURCE[0]}" ]
then
  echo "this script must be sourced to work, e.g.:"
  echo "  source mc-k8s.sh"
  exit 1
fi

export THIS_DIR=$(dirname "${BASH_SOURCE[0]}")

export HELM_EXPERIMENTAL_OCI=1
source <(helm completion bash)
source <(kubectl completion bash)

function mclist()
{
    # todo split into running and idle with extra info like node for running severs
    kubectl get deployment -n minecraft -o "custom-columns=MC SERVER NAME:metadata.annotations.meta\.helm\.sh/release-name,RUNNING:status.replicas"
}

function mccheckname()
{
    if [ -z "${1}" ]; then
      echo 'please supply an mc server name'
      return 1
    fi

    unset pod
    deployname="${1}"-minecraft
    deploy=$(kubectl get deploy -n minecraft -l app="${deployname}" -o name)

    if [ -z "${deploy}"  ]; then
        echo "${1} does not exist. Please use one of the following names:"
        mclist
        return 1
    fi

    pod=$(kubectl get pods -l app=${deployname} -o name)

    if [ "${pod}" ]; then
        return 0
    else
      # 2 means the server name exists but has no pod running
      return 2
    fi
}

function mccheck ()
{
    if mccheckname "${1}" ; then
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

    if [ -z "${pod}" ]; then
        # the server is not running, spin it up
        was_shutdown=true

        kubectl scale -n minecraft ${deploy} --replicas=1

        echo "starting ${deploy} ..."
        while [[ $(kubectl get -n minecraft pods -l app="${deployname}" -o 'jsonpath={..status.conditions[?(@.type=="Ready")].status}') != "True" ]]
        do
            sleep 1
        done

        pod=$(kubectl get pods -l app=${deployname} -o name)

        echo "waiting for minecraft server ..."
        while ! mccheck ${1}
        do
            sleep 1
        done
    else
        if [ "${deploy}" ]; then
            echo ${1} is already running
            export was_shutdown=false
        fi
    fi
}

function mcbackup()
{
    MCBACKUP=${MCBACKUP:-$(read -p "path to backup folder: " IN; echo $IN)}

    mcstart "${1}"

    if [[ "${pod}" ]]; then
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

# function mcwait()
# {
#     if [[ mccheckname ${1} != 1 ]]; then
#       while ! mccheck ${1}
#         do
#             sleep 1
#         done
#     fi
# }

function mcexec()
{
    if mccheck ${1}; then
      kubectl exec -it ${deploy} -- bash
    else
        echo "${1} is not running"
    fi
}

function mcstop()
{
    if mccheck ${1}; then
      kubectl scale --replicas=0 ${deploy}
    else
        echo "${1} is not running"
    fi
}

function mclog()
{
    if mccheckname "${1}"; then
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

function mctry()
{
    backupToTry=${1}
    helm template -f ${THIS_DIR}/giles-servers/tmp.yaml --set minecraftServer.eula=true,minecraftServer.downloadWorldUrl=${backupToTry} minecraft-server-charts/minecraft >/tmp/tmp-output.yaml
    helm upgrade --install tmp -f ${THIS_DIR}/giles-servers/tmp.yaml --set minecraftServer.eula=true,minecraftServer.downloadWorldUrl=${backupToTry} minecraft-server-charts/minecraft
}