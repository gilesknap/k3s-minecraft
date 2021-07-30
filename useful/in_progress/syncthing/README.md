Details
=======

I'm using syncthing to backup the etcd snapshots from my control plane
to my RAID array and also to make an extra redundant backup of my RAID array.

To get this to work simply install with helm

```
helm repo add k8s-at-home https://k8s-at-home.com/charts/
helm repo update
helm upgrade -i sync-pi1 k8s-at-home/syncthing -n admin -f values-pi1.yaml
helm upgrade -i sync-gknuc k8s-at-home/syncthing -n admin -f values-gknuc.yaml
```

then use the following to get a GUI that can configure syncthing
```
export POD_NAME=$(kubectl get pods --namespace admin -l "app.kubernetes.io/name=syncthing,app.kubernetes.io/instance=syncthing" -o jsonpath="{.items[0].metadata.name}")
echo "Visit http://127.0.0.1:8080 to use your application"
kubectl -n admin port-forward $POD_NAME 8080:8384
```

UPDATE: I have now setup ingress using ../ambassador

k8s&home
========
https://github.com/k8s-at-home

This is a great set of helm charts for installing stuff with a common chart
library for adding things like persistance etc.

values-pi1.yaml is a good example of a values file that makes use of the
k8s@home common library features.

Useful Docs:

| Subject | URL |
|---------|-----|
|All Application Charts| https://github.com/k8s-at-home/charts/tree/master/charts |
|Library values        | https://github.com/k8s-at-home/library-charts/tree/main/charts/stable/common |

snapshots
=========

To make this work required that the snapshots were accessible to the
syncthing account which is called abc with uid, guid 911.

on the cluster master (pi1 for me):
```
sudo mkdir /snapshots
sudo chown 911 /snapshots
sudo chgrp 911 /snapshots
sudo nano /etc/systemd/system/k3s.service
```

and add these two flags to the end of server startup
```
...
    server \
...
        --etcd-snapshot-retention 30 \
        --etcd-snapshot-dir /snapshots \
...
```

then test it with
```
systemctl daemon-reload
systemctl restart k3s.service
k3s etcd-snapshot
```

