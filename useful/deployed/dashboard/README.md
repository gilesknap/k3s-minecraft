# install the kubernetes dashboard

Update - I redeployed with these instuctions https://kubernetes.io/docs/tasks/access-application-cluster/web-ui-dashboard/

Then added a user and generated a token
```
k apply -f useful/deployed/dashboard/dashboard-admin.yaml
kubectl -n kubernetes-dashboard create token admin-user
```

Then run a proxy and use a proxy address to the dashboard service
```
nohup kubectl proxy &
http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy/#/service?namespace=default
```


# previous approach

Use the official helm chart

```bash
kubectl create namespace kubernetes-dashboard

# create Dashboard admin account
kubectl apply -f dashboard-admin.yaml

# Add kubernetes-dashboard repository
helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/
helm repo update

# Deploy a Helm Release named "dashboard-release" using the chart
helm upgrade -i dashboard-release -n kubernetes-dashboard kubernetes-dashboard/kubernetes-dashboard -f ../../pi_affinity.yaml -f dashboard-values.yaml  
# pi_affinity.yaml is to make it run on one of my 3 pi worker nodes
# dashboard-values.yaml increases token expiry
```

# discover the password
```
kubectl -n kubernetes-dashboard describe secret admin-user-token | grep '^token'
```

If you don't want to set up an ingress then you can use the dashboard right
away using a port forward like this (but see notes on certs next section)
```
export POD_NAME=$(kubectl get pods -n kubernetes-dashboard -l "app.kubernetes.io/name=kubernetes-dashboard,app.kubernetes.io/instance=dashboard-release" -o jsonpath="{.items[0].metadata.name}")
kubectl -n kubernetes-dashboard port-forward dashboard-release-kubernetes-dashboard-6f55b69bd7-h899s 8443:8443 --address='0.0.0.0'
# now browse to root of your port forwarding machine on port 8443
```
See [ingress-nginx](../ingress-nginx/README.md) for details of how to set up
an ingress so you do not require a proxy.

# Overcoming Chrome not liking the self signed Certificate
This is a pain - the best fix is to add an ingress with proper cert [see here](../ingress-nginx/)

But a crazy workaround is to click in the 'Unsafe' warning page and type
"thisisunsafe"

This worked on Chrome 98.0.4758.102 in Feb 2022.
