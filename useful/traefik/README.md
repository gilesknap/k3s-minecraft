Good Overview
=============

https://levelup.gitconnected.com/a-guide-to-k3s-ingress-using-traefik-with-nodeport-6eb29add0b4b

To enable traefik dashboard.
============================

essentially:
```
kubectl -n kube-system edit cm traefik
```
add:
[api]
  dashboard = true

before [metrics]

Then restart and use port forwarding to get a web interface
```
kubectl -n kube-system scale deploy traefik --replicas 0
kubectl -n kube-system scale deploy traefik --replicas 1

kubectl -n kube-system port-forward deployment/traefik 8080
```
and go to localhost:8080
