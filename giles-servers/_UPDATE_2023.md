Some infrastructure changes require fixes to deployments for them to run on
the current setup.

helm charts already fixed up are:

- most are done now

process (for e.g. mansion world)
```bash
k3
k delete -n minecraft -f giles-servers/pvcs/mansion-world.yaml
>> remove the /share in path in giles-servers/pvcs/mansion-world.yaml
k apply -n minecraft -f giles-servers/pvcs/mansion-world.yaml
>> change the node affinity as follows:-
    # don't run on the raspberry pis (as we have some nice Intel Nucs for this)
        nodeSelector:
        beta.kubernetes.io/arch: amd64
k8s-mcdeploy giles-servers/mansion-world.yaml
```
Also verify that you are using NodePort as per giles-servers/pvcs/noah-james.yaml
i.e.

```yaml
# use nodeport - cant use a loadbalancer with Google Wifi port forwarding :-(
serviceType: NodePort
nodePort: 30505

# enable rcon for remote control - set to false if not required
rcon:
# external port must be unique across all mc server instances
serviceType: NodePort
nodePort: 30506
enabled: true
```

That's it!


note: alias k3='export KUBECONFIG=/home/giles/.kube/mcconfig:/home/giles/.kube/config_argus:/home/giles/.kube/config_pollux; k config use-context minecraft; source /home/giles/work/k3s-minecraft/k8s-mc.sh'