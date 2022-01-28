# Debugging

# Name resolution issues

Also see https://kubernetes.io/docs/tasks/administer-cluster/dns-debugging-resolution/

This diagnosis undertaken because dashboard stopped working and I'm seeing this error
in the master
```
ssh pi1
journalctl -fu k3s

...
614 cacher.go:420] cacher (*unstructured.Unstructured): unexpected ListAndWatch error: failed to list acme.cert-manager.io/v1alpha2, Kind=Order: conversion webhook for acme.cert-manager.io/v1, Kind=Order failed: Post "https://cert-manager-webhook.cert-manager.svc:443/convert?timeout=30s": net/http: request canceled while waiting for connection (Client.Timeout exceeded while awaiting headers); reinitializing...
...

```

A nice trick to launch a debugging session in a pod on a particular node is as follows:
(the toleration is for the master node if needed)
```
kubectl run -it --rm --restart=Never busybox2 --image=busybox:1.28 --overrides='{"apiVersion": "v1", "spec": {"nodeSelector": { "kubernetes.io/hostname": "pi1" }, "tolerations": [{"key": "node-role.kubernetes.io/master", "operator": "Exists", "effect": "NoSchedule"}] }}' -- sh
```

To see if dns is working from the above busybox:
```
nslookup kubernetes.default
```

Using the above I was able to determine that master and gknuc were OK but that
the other pis were failing. This is consistent with the fact that I had just 
upgraded the pis to bullseye release of raspianOS.

I rebooted the offending pis and all was fixed. The error above was due to 
the certificate manager being out of touch with the master node since its DNS
was down.

