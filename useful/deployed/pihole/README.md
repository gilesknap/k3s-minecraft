# PiHole

Useful https://greg.jeanmart.me/2020/04/13/self-host-pi-hole-on-kubernetes-and-block-ad/

Here is a nice command which makes it easy to work out what you can change in 
a helm chart.

```
helm repo add mojo2600 https://mojo2600.github.io/pihole-kubernetes/
helm search repo mojo2600
helm show values mojo2600/pihole > pihole-values-original.yaml
```

Now deploy pihole
```
helm upgrade -i my-pihole mojo2600/pihole -n admin -f pihole-values.yaml
```

The values used enable dns service on a loadbalanced IP address (requires
metallb) and the admin interface on an ingress with hostname pi.hole
(requires ingress-nginx).


# PiHole Admin Interface

The ingress settings in piehole-values.yaml mean that you need only 
add pi.hole in /etc/hosts to point at one of the k8s nodes and you can browse
to http://pi.hole

Or you can use port forwarding as follows:
```
kubectl port-forward -n admin deployments/my-pihole 8002:80

```

Then browse to https://localhost:8002/admin

(please ignore the coincidence that I have used admin as the namespace and 
the root URL of the pihole admin interface is /admin)

before logging in you will need to create the admin password secret that
the pihole-values.yaml referred to.

kubectl create secret generic db-user-pass -n admin \
  --from-literal=username=admin \
  --from-literal=password='MyPasswordGoesHere'

# debugging load balancer issues

Working out how to get port 53 UDP and TCP forwarded to the pihole DNS service
proved to be difficult.

One way to see if this is working is to ssh into the node that is running
the pihole pod and use tcp dump:
```
sudo apt install tcpdump
sudo tcpdump 'tcp port 53'
# or 
sudo tcpdump 'udp port 53'
```