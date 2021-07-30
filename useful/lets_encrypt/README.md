This was an attempt to get some real certificates issued for use on
my domain using lets_encrypt.

https://runnable.com/blog/how-to-use-lets-encrypt-on-kubernetes

However the image used in the above article uses the V1 ACME protocol which
is deprecated.

This article using an alternative provider called Zero-SSL looks promising.
https://medium.com/@markmcwhirter/alternative-acme-via-cert-manager-a9e9e7f105e0

But it turns out that I got my self signed certs to work adequately within
my LAN, see ../ambassador/CERTIFICATE.md

When I come back to this eventually it may be worth pursuing the lets_encrypt
provider and find an up to date way of getting certs for the cluster. That is
because this seems to be the more popular service.
