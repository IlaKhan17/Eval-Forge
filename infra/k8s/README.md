# Kubernetes

A working deployment of the API, the worker, and the dashboard. Kustomize, no Helm — this is a
handful of resources, and a chart's templating language is a second thing to debug when the first
thing is already YAML.

```bash
kubectl create namespace proofstep
# Secrets first: nothing below starts without them.
kubectl -n proofstep create secret generic proofstep-secrets \
  --from-literal=jwt-secret="$(openssl rand -hex 32)" \
  --from-literal=database-url='postgresql+psycopg://proofstep_app:PASSWORD@HOST:5432/proofstep' \
  --from-literal=migration-database-url='postgresql+psycopg://proofstep:PASSWORD@HOST:5432/proofstep' \
  --from-literal=s3-secret-key='...'
kubectl apply -k infra/k8s
```

Check what you are about to apply before you apply it:

```bash
kubectl kustomize infra/k8s | kubectl apply --dry-run=client -f -
```

## What this does not include, and why

**Postgres, Redis, and object storage.** All three are stateful, all three are the part of the
system whose loss is unrecoverable, and running them in-cluster well is a specialism. Use RDS,
ElastiCache, and S3 — or their equivalents, or operators built for the job. Manifests here that
looked production-ready and were not would be worse than their absence.

**TLS certificates and DNS.** The Ingress names a host and a TLS secret and stops there, because how
those get created is a property of your cluster (cert-manager, a cloud load balancer, something
in front of it entirely) rather than of this application.

**Autoscaling.** A `HorizontalPodAutoscaler` needs a metric that reflects load and a tested scaling
behaviour. Ingestion is bursty and the worker's queue depth is the honest signal, not CPU — which
means a KEDA `ScaledObject` against Redis, not an HPA. Left out rather than shipped as a
CPU-threshold guess that scales the wrong component at the wrong time.

## The two things worth reading before you change anything

**`migrate` is a Job, and the Deployments wait for it.** Migrations do not run on pod startup. Three
replicas racing the same DDL is a deadlock at best, and a role that can create tables is a role that
can create one with no row-level-security policy — see `docs/HARDENING.md`. The Job runs as the
owning role; every long-lived pod runs as an application role that cannot reshape the schema.

**Two database URLs, deliberately.** `database-url` is the application role. `migration-database-url`
is the owner, and only the Job's pod ever sees it. Giving the API the owner's credential would put
the most privileged secret in the system into the process most exposed to the internet.
