# enable auto upgrade of k3s to latest stable

following instructions here https://rancher.com/docs/k3s/latest/en/upgrades/automated/

Get the (check for latest) YAML for deploying system-upgrade-controller
```
curl -sLO  https://github.com/rancher/system-upgrade-controller/releases/download/latest/system-upgrade-controller.yaml
```

Apply the file:
```
kubectl apply -f system-upgrade-controller.yaml
k apply -f upgradeplan.yaml -n system-upgrade

# use this to check for progress (-o yaml for more detail)
kubectl -n system-upgrade get jobs
```

In the plans we have used the stable channel.

You can see which versions are in which channel here:

https://update.k3s.io/v1-release/channels