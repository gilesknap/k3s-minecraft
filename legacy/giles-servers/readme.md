# SWITCHING TO NEW INFRASTRUCTURE

After reconfigure of my network I now have 2 changes to apply to all MC severs.

This is because

- NFS V4 does not require /share in front of paths (NAS is only serving v4 now)
- There is no loadbalancer anymore because Google Wifi can only port forward to 
  real machines (which is bobbins)

This example is for transformers-again

# Get rid of PV

This is for those MCs that have a permanent PV - this won't delete the data
as they are all set to reclaim policy 'Retain'.

```
kubectl patch pv/transformers-again-pv   -p '{"metadata":{"finalizers":[]}}' --type=merge
kubectl delete --force --grace-period 0 pv/transformers-again-pv
```

Fix up the files transformers-again.yaml and pvcs/transformers-again.yaml

Replace all /share with blank

```
kubectl apply -f  pvcs/transformers-again.yaml
```

# re-create the deployment

If you were using a loadbalancer previously change to NodePort

e.g. in transformers-again.yaml
```
  # Use NodePort because Google Wifi can't do loadbalancers
  serviceType: NodePort
  nodePort: 30511
```

Remove and re-create
```
helm delete transformers-again
k8s-mcdeploy gileservers/transformers-again.yaml
```