#!/bin/bash

POD=$(kubectl get pods | grep nginx | awk '{print $1}')
kubectl exec $POD -it bash
return 0

# in the pod run
apt-get update && apt-get install curl -qq -y
curl nginx # Name of the service
