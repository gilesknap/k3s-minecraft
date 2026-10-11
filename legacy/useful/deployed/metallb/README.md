# Metallb and Nginx Ingress

Attempting to get Pihole working with no metallb proved problematic.
Hence I want to try metallb instead of the default klipper
```
klipper: k3s default loadbalancer
traefik: k3s default ingress controller

metallb: much more widely used loadbalancer
ingress-nginx: much more widely used ingress controller
```
( see [ingress-nginx](../ingress-nginx/README.md) )

Metallb allows you to have an external address pool that services can draw on to
expose themselves outside of the cluster. Metallb will then load balance between
the replicas that the service is fronting.

# Installing Metallb on K3S

First we remove the defaults, klipper and traefik:

Update the file /etc/systemd/system/k3s.service and make the last ExecStart
look like this on the master node(s) :

```
ExecStart=/usr/local/bin/k3s \
    server \
    --disable servicelb \
    --disable traefik
```
after changes to the k3s.service file:
```
sudo systemctl daemon-reload
sudo systemctl restart k3s
```

UPDATE Feb 2024:
```
kubectl apply -f https://raw.githubusercontent.com/metallb/metallb/v0.14.3/config/manifests/metallb-native.yaml
kubectl apply -f metallb-config-2024.yaml
```

Install metallb with helm
(see https://bitnami.com/stack/metallb/helm)
note that the metallb-values.yaml is no longer needed as the default chart
works with hybrid cluster of arm and amd nodes.
```
helm repo add bitnami https://charts.bitnami.com/bitnami
helm upgrade -i my-metal bitnami/metallb -n metal --create-namespace
```

Now configure the range of IPs that metallb will use. Change the contents
of metallb-config to match a range of addresses your DHCP server will not use
(make necessary DHCP changes on the router first). Then:
```
k get configmap -n metal my-metal-metallb-config  -o yaml > metallb-config.yaml
# edit the yaml and add your config (example in the repo)
kubectl apply -f metallb-config.yaml

# any load balancers in the cluster should now be using one of these addresses:
# this should include the nginx-ingress controller
kubectl get services -o wide --all-namespaces | grep --color=never -E 'LoadBalancer|NAMESPACE'
```

For my existing ingresses that are using /etc/hosts to reach the cluster I now
see that they are using the first IP from the pool.

QUESTION: I'm not clear if I can rely on my ingress controller always picking up
the first IP address in the pool. If not I need some way of advertising the
correct IP for the host names in my ingresses.

ANSWER: when any service is configured with 'type: Loadbalancer' it may specify
it external IP with "loadBalancerIP:". Since I installed ingress first and did
not specify loadBalancerIP it picked the 1st available IP. So best to go back
and specify this in the ingress controller to enforce this. (Even better would
be to get external DNS working so that I would refer to ingresses by internal DNS
name only). For the moment I have added loadbalancerIP option to the installation
of [ingress-nginx](../ingress-nginx/README.md).

