Ambassador Ingress Controller
=============================

This details how to set up ingress for various services with Web GUI.

Note this means disabling traefik and replacing with `Ambassador Edge Stack`

See:
https://www.getambassador.io/docs/edge-stack/latest/tutorials/getting-started/


Install Ambassador
==================
Deploy ambassador as a lightweight ingress controller

https://rancher.com/blog/2020/deploy-an-ingress-controllers
discusses how to setup ingress for k3s but it is better to
 do it using helm as follows:
``` bash
# Add the Repo:
helm repo add datawire https://www.getambassador.io
helm repo update

# set up CRDS
# important - this will fail if you have arm nodes as it will try to run on them
# taint your arm nodes as follows (including the master if that is arm)
# kubectl taint nodes pi1 architecture=arm:NoSchedule
kubectl apply -f https://app.getambassador.io/yaml/emissary/latest/emissary-crds.yaml
kubectl wait --timeout=90s --for=condition=available deployment emissary-apiext -n emissary-system

# Create Namespace and Install:
kubectl create namespace ambassador
helm install ambassador --namespace ambassador datawire/ambassador
kubectl -n ambassador wait --for condition=available --timeout=90s deploy -lproduct=aes
```

See here for details on how to create Ambassador Edge Stack 'mappings'.
https://www.getambassador.io/docs/edge-stack/latest/topics/using/intro-mappings/

Install Mappings for my Services
================================

``` bash
# syncthing
k apply -f syncthing-mapping.yaml
# k8s dashboard
k apply -f dashboard-mapping.yaml
```

Interesting
===========

Ambassador can also be used with ingress but claims that mappings are
superior:

https://www.getambassador.io/docs/edge-stack/latest/topics/running/ingress-controller/
