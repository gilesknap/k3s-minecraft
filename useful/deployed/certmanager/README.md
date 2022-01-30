# setup Certificate Manager for Lets Encrypt

## set up a Cert Manager
To secure the connection you need to generate an SSL cert for the site.
I used these notes to set up a cert manager with lets-encrypt certificates.

https://opensource.com/article/20/3/ssl-letsencrypt-k3s

I did these steps (check for an updated release at https://github.com/jetstack/cert-manager/tags first)
``` bash
kubectl create namespace cert-manager
curl -sL \
https://github.com/jetstack/cert-manager/releases/download/v1.6.1/cert-manager.yaml |\
sed -r 's/(image:.*):(v.*)$/\1-arm:\2/g' > cert-manager-arm.yaml
```

For my mixed node cluster we need to add affinity to arm.

Add the following yaml into the template for each of the 3 deployments - at the
same indent level as `containers:`
``` yaml
      affinity:
        nodeAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            nodeSelectorTerms:
            - matchExpressions:
              - key: kubernetes.io/arch
                operator: In
                values:
                - arm64
```

Now apply the updated yaml
```
kubectl apply -f cert-manager-arm.yaml
```

OK - that was easy !

## Stage Cert Manager with Lets Encrypt

Continuing with instructions from https://opensource.com/article/20/3/ssl-letsencrypt-k3s

I have updated their letsencrypt-issuer-staging.yaml in this repo with 
nginx ingress and latest API version.

```
kubectl apply -f letsencrypt-issuer-staging.yaml
# verify it is working with 
kubectl get clusterissuers
```

## Request test a Certificate

```
kubectl apply -f le-test-certificate.yaml
kubectl get certificates --all-namespaces # until READY = True

# diagnose issues with
kubectl describe certificate gilesk-ddns-net
kubectl describe certificaterequest gilesk-ddns-net-XXXXXX
kubectl describe orders default/gilesk-ddns-net-XXXXXX-XXXX
kubectl describe challenges gilesk-ddns-net-XXXXXX-XXXX....

# if the tests has worked then delete the test resources
kubectl delete certificates gilesk-ddns-net
kubectl delete secrets gilesk-ddns-net-tls
```

## Configure production cert manager

```
kubectl apply -f lets-encrypt-issuer-production.yaml
# then update the ingress to use TLS - this will automatically request a cert
kubectl apply -f ../ingress-nginx/dash-ingress-tls.yaml
```

# What to do with Router and exposing ports on the internet
Now I removed the router's port forward and added an /etc/hosts entry for the 
server on my client machine. Thus my dashboard is not exposed on the 
internet. I was not sure if this would break certificate requests but checked 
and apparently not (no probably I never needed the port forward - unless
I do want to expose services on the internet)

## Next challenge
How do I expose some services but not others? I guess I could
do filtering at the router - but it feels like k8s should be able to do this
for me. Ingresses usually only work on 80 and 443 but maybe it is possible to
set up >1 ingress with different ports?

ANSWER: Just use different URLs and put them in
etc/hosts for internal and register them with noip for external. Then do
a router port forward of 80 and 443 to one of the cluster nodes. 

