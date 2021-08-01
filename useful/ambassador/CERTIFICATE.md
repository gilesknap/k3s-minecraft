TRY THIS https://runnable.com/blog/how-to-use-lets-encrypt-on-kubernetes

So far I've tried this but it looks like this can only sign certs
with organization system:nodes which probably wont work for browsers.


Set up a Certificate for the site
=================================

See
- https://kubernetes.io/docs/tasks/tls/managing-tls-in-a-cluster/
- https://www.getambassador.io/docs/edge-stack/latest/howtos/tls-termination/

generate a certificate request record
``` json
cat <<EOF | cfssl genkey - | cfssljson -bare server
{
  "hosts": [
    "gknuc.local",
    "gilesk.ddns.net",
    "192.168.86.32",
    "86.20.126.87"
  ],
  "CN": "gknuc.local",
  "key": {
    "algo": "ecdsa",
    "size": 256
  },
  "names": [
    {
      "O": "gilesk"
    }
  ]
}
EOF
```

send the request to kubernetes
``` json
cat <<EOF | kubectl apply -f -
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: dashboard-mapping.kubernetes-dashboard
spec:
  request: $(cat server.csr | base64 | tr -d '\n')
  signerName: kubernetes.io/kubelet-serving
  usages:
  - digital signature
  - key encipherment
  - server auth
EOF
```

approve the request
```
kubectl certificate approve dashboard-mapping.kubernetes-dashboard
```

create a secret with the new cert
```
kubectl get csr dashboard-mapping.kubernetes-dashboard -o jsonpath='{.status.certificate}'     | base64 --decode > server.crt
kubectl create secret tls tls-cert --cert=server.crt --key=server-key.pem
```

add it to ambassador
``` yaml
cat <<EOF | kubectl apply -f -
apiVersion: getambassador.io/v2
kind: Host
metadata:
  name: wildcard-host
spec:
  hostname: "*"
  acmeProvider:
    authority: none
  tlsSecret:
    name: tls-cert
  selector:
    matchLabels:
      hostname: wildcard-host
EOF
```

add the signing authority to Chrome
``` bash
# copy out the server ca cert from the cluster master
ssh pi1
sudo -i
scp  /var/lib/rancher/k3s/server/tls/server-ca.crt giles@gklinux2:/home/giles
# get chrome to accept above (don't forget to switch to the ROOT auth tab)
```

This seems to have worked for the following examples:
- http://gilesk.ddns.net/dashboard/
- http://gilesk.ddns.net/hello
- http://gknuc.local/dashboard/

Note that a better way to do this may be to use TLSContext. See the end of :
- https://www.getambassador.io/docs/edge-stack/latest/topics/running/ingress-controller/




