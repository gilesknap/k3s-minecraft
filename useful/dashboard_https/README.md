Attempting to provide HTTPS ingress to the Dashboard
====================================================

and other services like syncthing.

Install Ambassador
==================
Deploy ambassador as a lightweight ingress controller

See https://rancher.com/blog/2020/deploy-an-ingress-controllers
but actually do it using helm as follows:
``` bash
# Add the Repo:
helm repo add datawire https://www.getambassador.io

# Create Namespace and Install:
kubectl create namespace ambassador && \
helm install ambassador --namespace ambassador datawire/ambassador && \
kubectl -n ambassador wait --for condition=available --timeout=90s deploy -lproduct=aes
```

See here for details on how to create Ambassador Edge Stack 'mappings'.
https://www.getambassador.io/docs/edge-stack/latest/topics/using/intro-mappings/

Install Mappings for Dash and Sync
==================================

```
k apply -f syncthing-mapping.yaml
k apply -f dashboard.yaml
```
