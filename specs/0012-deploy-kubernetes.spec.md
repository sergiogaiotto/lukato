# SPEC-0012 — Empacotamento e implantacao em Kubernetes

> **Status:** aceito · **Depende de:** SPEC-0001 · **Normativo.**

## 1. Imagem

Multi-stage `python:3.11-slim-bookworm`; runtime sem toolchain de build; usuario
`10001:10001` (non-root); `tini` como PID 1; `HEALTHCHECK` em `/healthz`;
entrypoint com os comandos `serve`, `migrate`, `seed`, `shell`.

## 2. Manifestos (`deploy/k8s/`)

```text
deploy/k8s/
├── base/
│   ├── kustomization.yaml
│   ├── namespace.yaml          serviceaccount.yaml
│   ├── configmap.yaml          secret.example.yaml
│   ├── deployment.yaml         service.yaml
│   ├── hpa.yaml                pdb.yaml
│   ├── networkpolicy.yaml      ingress.yaml
│   ├── job-migrate.yaml        servicemonitor.yaml
└── overlays/
    ├── dev/kustomization.yaml
    └── prod/kustomization.yaml  (replicas, recursos, HSTS, auth obrigatoria)
```

## 3. Requisitos do Deployment

* `replicas: 2` (dev) / `3` (prod); `RollingUpdate` `maxUnavailable: 0`, `maxSurge: 1`.
* Probes: `startupProbe` (`/healthz`, `failureThreshold: 30`, `periodSeconds: 2`),
  `livenessProbe` (`/healthz`), `readinessProbe` (`/readyz`).
* `resources`: requests `250m/512Mi`, limits `1000m/1Gi`.
* `securityContext`: `runAsNonRoot`, `runAsUser: 10001`, `allowPrivilegeEscalation: false`,
  `readOnlyRootFilesystem: true`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`.
  Volumes `emptyDir` para `/tmp` e `/app/var`.
* `terminationGracePeriodSeconds: 30`; `preStop` com `sleep 5` para drenar o balanceador.
* `topologySpreadConstraints` por `kubernetes.io/hostname`.
* Segredos **somente** via `secretKeyRef` (`lukato-secrets`): chave do LLM, chave de
  embeddings, `JWT_SECRET`, credenciais Langfuse, URL do banco.
  `secret.example.yaml` contem **placeholders** e um comentario apontando para
  ExternalSecrets/Vault. Nenhum segredo real e versionado.
* `Job` de migracao com `helm.sh/hook`-equivalente (`argocd.argoproj.io/hook: PreSync`)
  executando `entrypoint.sh migrate`.
* `HPA` v2: CPU 70% e `lukato_http_requests_total` (opcional, via adapter);
  `minReplicas: 2`, `maxReplicas: 10`.
* `PodDisruptionBudget: minAvailable: 1`.
* `NetworkPolicy`: egresso para PostgreSQL, hub de LLM/embeddings, Langfuse e DNS;
  ingresso apenas do controlador de Ingress.

## 4. Criterios de aceite

1. `kubectl kustomize deploy/k8s/overlays/dev` renderiza sem erro.
2. Nenhum segredo real em nenhum arquivo versionado.
3. O container roda como non-root com root filesystem somente-leitura.
4. `docker build` conclui e `docker run` responde `200` em `/healthz`.
