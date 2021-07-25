Not Yet Working
===============
This is pretty much working but I got stalled on trying to get ingress
to work for it. See ../dashboard_https - that work is stalled on getting
TLS to work and might already have enough info to make ingress for this
tool work.


Details
=======

I'm using syncthing to backup the etcd snapshots from my control plane
to my RAID array and also to make an extra redundant backup of my RAID array.

To get this to work simply install with

```
helm repo add k8s-at-home https://k8s-at-home.com/charts/
helm repo update
helm upgrade -i syncthing k8s-at-home/syncthing -n admin
```


then use the following to get a GUI that can configure syncthing
```
export POD_NAME=$(kubectl get pods --namespace admin -l "app.kubernetes.io/name=syncthing,app.kubernetes.io/instance=my-syncthing" -o jsonpath="{.items[0].metadata.name}")
echo "Visit http://127.0.0.1:8080 to use your application"
kubectl port-forward $POD_NAME 8080:8384
```

