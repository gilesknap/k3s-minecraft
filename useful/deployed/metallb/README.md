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

To remove klipper and traefik:

Update the file /etc/systemd/system/k3s.service and make the last ExecStart 
look like this:

```
ExecStart=/usr/local/bin/k3s \
    server \
    --no-deploy traefik \
    --disable servicelb \
    --disable traefik
```

Install metallb with helm
(see https://bitnami.com/stack/metallb/helm)
```
helm repo add bitnami https://charts.bitnami.com/bitnami
helm upgrade -i my-metal bitnami/metallb -n metal --create-namespace -f metallb-values.yaml
```

Now configure the range of IPs that metallb will use. Change the contents 
of metallb-config to match a range of addresses your DHCP server will not use
(make necessary DHCP changes on the router first). Then:
```
kubectl apply -f metallb-config.yaml

# any load balancers in the cluster should now be using one of these addresses:
# this should include the nginx-ingress controller
kubectl get services -o wide --all-namespaces | grep --color=never -E 'LoadBalancer|NAMESPACE'
```

For my existing ingresses that are using /etc/hosts to reach the cluster I now
see that they are using the first IP from the pool.

TODO: I'm not clear if I can rely on my ingress controller always picking up
the first IP address in the pool. If not I need some way of advertising the 
correct IP for the host names in my ingresses. 

