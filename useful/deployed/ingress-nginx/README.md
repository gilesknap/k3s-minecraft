These links were useful in working this out

   - https://kubernetes.github.io/ingress-nginx/deploy/
   - https://blog.thenets.org/how-to-create-a-k3s-cluster-with-nginx-ingress-controller/


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
