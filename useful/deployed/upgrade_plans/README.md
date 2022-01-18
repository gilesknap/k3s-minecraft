# enable auto upgrade of k3s to latest stable

following instructions here https://rancher.com/docs/k3s/latest/en/upgrades/automated/

Get the (check for latest) YAML for deploying system-upgrade-controller
```
curl -sL  https://github.com/rancher/system-upgrade-controller/releases/download/v0.8.1/system-upgrade-controller.yaml > system-upgrade-controller-v0.8.1.yaml
```

Edit above file and add in my tolerations for raspis, then:
```
kubectl apply -f system-upgrade-controller-v0.8.1.yaml
k apply -f upgradeplan.yaml -n system-upgrade

# use this to check for progress (-o yaml for more detail)
kubectl -n system-upgrade get jobs
```
