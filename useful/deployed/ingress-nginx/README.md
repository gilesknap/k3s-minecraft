# Setting up Ingress
## in particular to expose the Dashboard without requiring a Proxy

(but a good ingress controller is also useful for setting up any web
services on http/https that you expose outside of the cluster)

These links were useful in working this out

   - https://kubernetes.github.io/ingress-nginx/deploy/
   - https://blog.thenets.org/how-to-create-a-k3s-cluster-with-nginx-ingress-controller/
   - https://jonathangazeley.com/2020/09/16/exposing-the-kubernetes-dashboard-with-an-ingress/
   - https://stackoverflow.com/questions/48324760/ingress-configuration-for-dashboard

## Get nginx ingress working

I eventually chose to replace the ingress and loadbalancer of k3s with more popular options.
The instruction below are for replacing the ingress only. Read 
[here](../metallb/README.md) first for details of replacing the loadbalancer too.

To disable the built in traefik ingress controller add the --no-deploy option
below to `/etc/systemd/system/k3s.service` on the master node and 
`sudo systemctl restart k3s`
```
ExecStart=/usr/local/bin/k3s \
    server \
    --no-deploy traefik 
```

Install ingress-nginx with the following command. If you are using metallb then
it would be useful to specify which ip address you want the ingress assigned to.
If you are still using klipper (k3s default loadbalancer) then the ingress 
will respond to any node ip address in the cluster.

Remove controller.service.loadBalancerIP if you are using klipper or 
change the IP as required if using metallb.
```
helm upgrade --install ingress-nginx ingress-nginx \
  --repo https://kubernetes.github.io/ingress-nginx \
  --namespace ingress-nginx --create-namespace \
  --set controller.service.loadBalancerIP=192.168.86.50
```

Verify it is up and running with:
```
kubectl wait --namespace ingress-nginx \
  --for=condition=ready pod \
  --selector=app.kubernetes.io/component=controller \
  --timeout=120s
```

## Create insecure ingress for Dashboard
Now you can get dashboard ingress working in an unsecure fashion using
[this yaml](dash-ingress.yaml).

```
kubectl apply -f dash-ingress.yaml
```

This allows me to see my dashboard login at 

- http://gknuc.local
- http://gilesk.ddns.net

The second one is my public dns name provided by noip and port forwarded into
one of my servers. Therefore it is a bad idea to send the login token over
this connection until we secure with https.

Next Up set up a cert manager so we can have a real certificate to provide
SSL to out public address.

See [CertManager](../certmanager/README.md)


