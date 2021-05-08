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

format=custom-columns=Name:metadata.labels.app\
,GameMode:spec.containers[0].env[22].value\
,Server:spec.nodeName\
,IP:status.hostIP\
,Rcon:spec.containers[0].ports[0].containerPort

function mclist()
{
    # list the minecraft servers deployed in the cluster with useful status info
    echo == Running Minecraft Servers ==
    # filter out svclb pods (which all have pod-template-generation=1
    # meh - really I should fix the helm chart to label the MC pods)
    kubectl -n minecraft get pods -l pod-template-generation!=1 -o $format
    echo
    # incredibly I cant see how to filter deployments on no. of replicas so do a loop
    echo == Idle Minecraft Servers ==
    for d in $(kubectl -n minecraft get deploy -o name)
    do
        if [ "$(kubectl get ${d} -o jsonpath={.status.replicas})" != "1" ]
        then
          kubectl get ${d} -o jsonpath='{.metadata.labels.app}{"\n"}'
        fi
    done
}

function mccheckname()
{
    # verify that a minecraft server name exists in the cluster
    # set pod to the name of the pod it is running in if it is active
    # set deploy to the name of its deployment
    unset pod deploy

    if [ -z "${1}" ]; then
      echo 'please supply an mc server name'
      return 1
    fi

    shortname="${1}"
    deployname="${1}"-minecraft
    deploy=$(kubectl get deploy -n minecraft -l app="${deployname}" -o name)

    if [ -z "${deploy}"  ]; then
        echo "${1} does not exist. Please use one of the following names:"
        echo
        mclist
        return 1
    fi

    # if there is a pod associated with the app name then set $pod to its name
    pod=$(kubectl get pods -l app=${deployname} -o name)
}

function mccheck ()
{
    # verify that a minecraft server is running
    # checks that the pod is active and checks the health of the server
    if mccheckname "${1}" ; then
        if [ ${pod} ]; then
            # the pod is running - check the state of minecraft server
            output=$(kubectl exec -n minecraft ${pod} -- /health.sh)
            echo "${output}"
            # a good result contains a motd
            if [[ "${output}" == *"motd"* ]]; then
                return 0
            fi
        fi
    fi
    return 1
}


function mcstart()
{

    if mccheck "${1}" ; then
        echo "${1}" is already running
        export was_shutdown=false
    else
      if [ ${deploy} ]; then
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
      fi
    fi
}

function mcbackup()
{
    # backup a minecraft server to a zip file
    MCBACKUP=${MCBACKUP:-$(read -p "path to backup folder: " IN; echo $IN)}

    mcstart "${1}"

    if [ "${pod}" ]; then
        zipname=$(date +%Y-%m-%d-%X)-${shortname}.zip

        kubectl exec -n minecraft ${pod} -- rcon-cli save-off
        kubectl exec -n minecraft ${pod} -- rcon-cli save-all
        tmp_dir=$(mktemp -d)
        kubectl -n minecraft cp ${pod#pod/}:/data ${tmp_dir}
        zip -r ${MCBACKUP}/${zipname} ${tmp_dir}
        rm -r ${tmp_dir}
        kubectl -n minecraft exec ${pod} -- rcon-cli save-on

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
    # execute bash in the container for server $1
    # for debugging and also can directly edit server.properties

    if mccheck ${1}; then
      kubectl exec -it ${deploy} -- bash
    else
        echo "${1} is not running"
    fi
}

function mcstop()
{
    # stop the minecraft server named $1
    if mccheck ${1}; then
      kubectl scale --replicas=0 ${deploy}
    else
        echo "${1} is not running"
    fi
}

function mclog()
{
    # see the server log for server $1
    # add -f to attach to the log stream
    if mccheckname "${1}"; then
        shift
        kubectl logs ${deploy} ${*}
    fi
}

function mcdeploy()
{
    # deploy a minecraft server based on the helm chart override values in file $1
    # the release name (used by all mc functions in this script to identify server)
    # is taken from the basename of the file
    # To use: copy minecraft-helm.yaml to my-new-server-name.yaml and edit it as
    # required, then mcdeploy my-new-server-name.yaml

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
    # try out a world backup in the 'tmp' deployment
    # overwrites the previous tmp deployment with the world defined in a zip or folder provided in $1
    backupToTry=${1}
    helm template -f ${THIS_DIR}/giles-servers/tmp.yaml --set minecraftServer.eula=true,minecraftServer.downloadWorldUrl=${backupToTry} minecraft-server-charts/minecraft >/tmp/tmp-output.yaml
    helm upgrade --install tmp -f ${THIS_DIR}/giles-servers/tmp.yaml --set minecraftServer.eula=true,minecraftServer.downloadWorldUrl=${backupToTry} minecraft-server-charts/minecraft
}