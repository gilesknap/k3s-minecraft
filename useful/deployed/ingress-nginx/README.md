# Setting up Ingress
## in particular to expose the Dashboard without requiring a Proxy
These links were useful in working this out

   - https://kubernetes.github.io/ingress-nginx/deploy/
   - https://blog.thenets.org/how-to-create-a-k3s-cluster-with-nginx-ingress-controller/
   - https://jonathangazeley.com/2020/09/16/exposing-the-kubernetes-dashboard-with-an-ingress/
   - https://stackoverflow.com/questions/48324760/ingress-configuration-for-dashboard

## Get nginx ingress working

Install ingress-nginx with:
```
helm upgrade --install ingress-nginx ingress-nginx \
  --repo https://kubernetes.github.io/ingress-nginx \
  --namespace ingress-nginx --create-namespace
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


