# install the kubernetes dashboard

Use the official helm chart

```bash
kubectl create namespace kubernetes-dashboard

# create Dashboard admin account
kubectl apply -f dashboard-admin.yaml

# Add kubernetes-dashboard repository
helm repo add kubernetes-dashboard https://kubernetes.github.io/dashboard/
helm repo update

# Deploy a Helm Release named "dashboard-release" using the chart
helm install dashboard-release -n kubernetes-dashboard kubernetes-dashboard/
kubernetes-dashboard -f ../../pi_affinity.yaml
# pi_affinity.yaml is to make it run on one of my 3 pi worker nodes
```

# discover the password
```
kubectl -n kubernetes-dashboard describe secret admin-user-token | grep '^token'
```

# use a proxy to browse the dashboard
Finally, start a proxy and goto the Dashboard URL, use the above token to log in.
```
kubectl proxy &
browse to http://localhost:8001/api/v1/namespaces/kubernetes-dashboard/services/https:kubernetes-dashboard:/proxy
```
See [ingress-nginx](../ingress-nginx/README.md) for details of how to set up
an ingress so you do not require a proxy.
