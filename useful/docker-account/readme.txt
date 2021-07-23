The cluster may pull from dockerhub quite often and hit limits on pulls:

anonymous: 100 per 6 hours
free: 200 per 6 hours
paid: unlimited

Add your docker credentials to a cluster secret to improve your limits by running
./add-docker-secret.switch

Update each deployment that you want to use the creds with

spec:
  ...
  template:
    ...
    spec:
      imagePullSecrets:
        - name: regcred

You will need to redoply the secret into the same namespace as the deployment.