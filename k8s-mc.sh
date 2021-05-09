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

###############################################################################
## Helper Functions
###############################################################################

function k8s-mccheckname()
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
        k8s-mclist
        return 1
    fi

    # if there is a pod associated with the app name then set $pod to its name
    pod=$(kubectl get pods -l app=${deployname} -o name)
}

function k8s-mccheck ()
{
    # verify that a minecraft server is running
    # checks that the pod is active and checks the health of the server
    if k8s-mccheckname "${1}" ; then
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
function k8s-mcvalidyaml()
{
    # verify that a yaml file is a valid helm substitution
    if [[ $(helm template -f "${1}"  minecraft-server-charts/minecraft) == *NAME-minecraft* ]]
    then
      return 0
    fi
    return 1
}

function k8s-mcvalidbackup()
{
    # verify that a minecraft backup is valid (or at least has a level.dat file)
    k8smcbackupFileName="${1}"

    MCBACKUP=${MCBACKUP:-$(read -p "path to backup folder: " IN; echo $IN)}

    case "${k8smcbackupFileName}" in
    /*)
        # full path with leading slash - leave it alone
        ;;
    *)
        k8smcbackupFileName=${MCBACKUP}/"${k8smcbackupFileName}"
        ;;
    esac

    case "${k8smcbackupFileName}" in
    *.zip)
        if grep -iq "level.dat" < <( unzip -l "${k8smcbackupFileName}" 2>/dev/null); then
            return 0
        fi
        ;;
    *)
        # assume this is a folder
        if [[ -n $(find  ${MCBACKUP} -name level.dat) ]]; then
          return 0
        fi
        ;;
    esac

    return 1
}

function k8s-mcwait()
{
    # block until an mc server has good health

    # first wait for the pod to be active
    while [[ $(kubectl get -n minecraft pods -l app="${deployname}" -o 'jsonpath={..status.conditions[?(@.type=="Ready")].status}') != "True" ]]
    do
        sleep 1
    done

    # get the pod name
    pod=$(kubectl get pods -l app=${deployname} -o name)

    # wait for the mc server to be healthy
    echo "waiting for minecraft server ${1}"
    if k8s-mccheckname ${1}; then
      while ! k8s-mccheck 2>/dev/null ${1}
        do
            sleep 1
        done
    fi
}

###############################################################################
## User Functions
###############################################################################

format=custom-columns=\
NAME:metadata.labels.release\
,MODE:spec.template.spec.containers[0].env[22].value\
,VERSION:spec.template.spec.containers[0].env[2].value\
,SERVER:'spec.template.spec.nodeSelector.kubernetes\.io/hostname'\
,RUNNING:status.availableReplicas

function k8s-mclist()
{
    # list the minecraft servers deployed in the cluster with useful status info

    kubectl -n minecraft get deploy -o $format
    echo
}

portsformat=custom-columns=\
PORT:spec.template.spec.containers[0].ports[0].containerPort,\
NAME:metadata.ownerReferences[0].name

function k8s-mcports()
{
  # list all server and rcon ports
  kubectl -n minecraft get daemonsets.apps -o ${portsformat}\
    --sort-by=.spec.template.spec.containers[0].ports[0].containerPort
}

function k8s-mcstart()
{

    if k8s-mccheck "${1}" ; then
        echo "${1}" is already running
        export was_shutdown=false
    else
      if [ ${deploy} ]; then
        # the server is not running, spin it up
        was_shutdown=true

        echo "starting ${deploy} ..."
        kubectl scale -n minecraft ${deploy} --replicas=1

        k8s-mcwait ${1}
      fi
    fi
}

function k8s-mcstop()
{
    # stop the minecraft server named $1
    if k8s-mccheck ${1}; then
      kubectl scale --replicas=0 ${deploy}
    else
        echo "${1} is not running"
    fi
}

function k8s-mcexec()
{
    # execute bash in the container for server $1
    # for debugging and also can directly edit server.properties

    if k8s-mccheck ${1}; then
      kubectl exec -it ${deploy} -- bash
    else
        echo "${1} is not running"
    fi
}

function k8s-mclog()
{
    # see the server log for server $1
    # add -f to attach to the log stream
    if k8s-mccheckname "${1}"; then
        shift
        kubectl logs ${deploy} ${*}
    fi
}

function k8s-mcdeploy()
{
    # deploy a minecraft server based on the helm chart override values in file $1
    # the release name (used by all k8s-mc functions in this script to identify server)
    # is taken from the basename of the file
    # To use: copy minecraft-helm.yaml to my-new-server-name.yaml and edit it as
    # required, then k8s-mcdeploy my-new-server-name.yaml

    filename="${1}"
    if k8s-mcvalidyaml ${filename}; then
        base=$(basename ${filename})
        releasename="${base%.*}"
        MCPASSWD=${MCPASSWD:-$(read -p "password for rcon: " IN; echo $IN)}
        MCEULA=${MCEULA:-$(read -p "agree to minecraft EULA? (type 'true'): " IN; echo $IN)}
        helm repo add minecraft-server-charts https://itzg.github.io/minecraft-server-charts/
        helm upgrade --install ${releasename} -f ${filename} --set minecraftServer.eula=${MCEULA},rcon.password="${MCPASSWD}" minecraft-server-charts/minecraft
        k8s-mcwait $releasename
    else
        echo "please supply a valid helm values override file for parameter 1 (see example dashboard-admin.yaml)"
    fi
}

###############################################################################
## Backup and Restore Functions
###############################################################################

function k8s-mcbackups()
{
    # list the backups in the $MCBACKUP folder

    # you can refer to these in k8s-mcrestore or k8s-mcbackup
    # without using full path

    MCBACKUP=${MCBACKUP:-$(read -p "path to backup folder: " IN; echo $IN)}
    echo "MCBACKUP folder is ${MCBACKUP}"
    ls -R ${MCBACKUP}
}

function k8s-mcbackup()
{
    # backup a minecraft server to a zip file
    MCBACKUP=${MCBACKUP:-$(read -p "path to backup folder: " IN; echo $IN)}

    k8s-mcstart "${1}"

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
function k8s-mcrestore()
{
    # restore a backup into an exisitng server deployment

    # IMPORTANT - the backup folder must be mounted read only in the
    # server pods, see extraVolumes in minecraft-helm.yaml

    filename="${1}"

    read -p "WARNING this will overwrite the current world. OK? " -n 1 -r
    echo

    if [[ $REPLY =~ ^[Yy]$ ]]; then

        if ! k8s-mcvalidbackup ${2}; then
            echo "please name a valid zipped minecraft save from ${MCBACKUP} as a parameter 2"
            return 1
        fi

        if k8s-mcvalidyaml ${filename}; then
            restore_settings="--set extraEnv.FORCE_WORLD_COPY=true,minecraftServer.downloadWorldUrl=${k8smcbackupFileName},minecraftServer.eula=true"

            echo $restore_settings

            base=$(basename ${filename})
            releasename="${base%.*}"
            helm repo add minecraft-server-charts https://itzg.github.io/minecraft-server-charts/
            helm upgrade ${releasename} -f ${filename} ${restore_settings} minecraft-server-charts/minecraft

            # reset the FORCE_WORLD_COPY so future changes will be preserved on restart
            k8s-mcwait ${releasename}
            kubectl set env deployments.apps/${releasename}-minecraft FORCE_WORLD_COPY=false
        else
            echo "please supply a valid helm values override file for parameter 1 (see example dashboard-admin.yaml)"
        fi
    fi
}

###############################################################################
## Experimental
###############################################################################

function k8s-mctry()
{
    # try out a world backup in the 'tmp' deployment
    # overwrites the previous tmp deployment with the world defined in a zip or folder provided in $1

    if ! k8s-mcvalidbackup "${1}"; then
        echo "please supply a valid zipped minecraft save as a parameter 1"
        return 1
    fi
    helm upgrade --install tmp -f ${THIS_DIR}/giles-servers/tmp.yaml --set minecraftServer.eula=true,minecraftServer.downloadWorldUrl=${k8smcbackupFileName} minecraft-server-charts/minecraft
    k8s-mcwait tmp
}

###############################################################################
## Help
###############################################################################

function k8s-mchelp()
{
    echo """
    This script adds the following functions to bash. It assumes that your
    servers have been deployed using:
        https://itzg.github.io/minecraft-server-charts/minecraft

    In all cases below <server name> is the helm chart release name.

    k8s-mclist
        list all minecraft servers deployed to the cluster

    k8s-mcports
        details of the ports exposed by servers and rcon

    k8s-mcstart <server name>
        start the server (set replicas to 1)

    k8s-mcstop <server name>
        stop the server (set replicas to 0)

    k8s-mcexec <server name>
        execute bash in the server's container

    k8s-mclog <server name> [-p] [-f]
        get logs for the server
        -p = log for previous instance after a restart of the pod
        -f = attach to the log and monitor it

    k8s-mcdeploy <my_server_def.yaml>
        deploys a server to the cluster with release name my_server_def
        recommended: copy and edit minecraft-helm.yaml for most common options,
        see in file comments for details.

    k8s-mcbackups
        List the backups in the folder $MCBACKUP

    k8s-mcbackup <server name>
        zips up the server data folder to a dated zip file and places it
        in the folder $MCBACKUP (prompts for MCBACKUP if not set)

    k8s-mcrestore <server name> <backup file name>
        restore the world from backup for a server, overwriting its
        current world

    """
}
