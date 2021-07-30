#!/bin/bash

docker login
kubectl -n default create secret generic regcred --from-file=.dockerconfigjson=/home/${USER}/.docker/config.json --type=kubernetes.io/dockerconfigjson
kubectl -n default get secret regcred --output=yaml