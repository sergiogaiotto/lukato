# Overlay OKE — o que preencher antes de aplicar

Este overlay **nao aplica como esta**. Todo valor `<PREENCHER: ...>` depende da
tenancy e do cluster. O runbook completo (banco, segredos, validacao, rollback)
esta em [`docs/DEPLOY.md`](../../../../docs/DEPLOY.md); aqui fica so a lista de
preenchimento.

## 1. Perguntas ao time de plataforma (antes de editar qualquer arquivo)

| # | Pergunta | O que trava se ficar sem resposta |
|---|---|---|
| 1 | Region key: `gru` (sa-saopaulo-1) ou `vcp` (sa-vinhedo-1)? | URL da imagem |
| 2 | Qual o **tenancy namespace** (Object Storage namespace)? | URL da imagem e login no OCIR |
| 3 | A tenancy usa identity domains / federacao IDCS? | Formato do usuario no `docker login`: `<ns>/<user>` vs `<ns>/<dominio>/<user>` — causa numero 1 de `unauthorized` |
| 4 | O repositorio OCIR do lukato ja existe? Em qual compartment? | `push` pode falhar por falta de `REPOSITORY_MANAGE` |
| 5 | Cluster **basic** ou **enhanced**? | Workload identity (ESO sem chave estatica) so funciona em enhanced |
| 6 | Quais as labels de Pod Security Admission do namespace destino? Ha Kyverno/Gatekeeper? | Pod recusado na admissao |
| 7 | Qual ingress ja esta padronizado: NIC, nginx, ou nenhum? | Escolha entre Ingress e Service `type=LoadBalancer` |
| 8 | Ingress **publico ou privado**? Quem emite o certificado TLS? | `oci-load-balancer-internal` e **imutavel** depois de criado |
| 9 | Ha quota de Load Balancer disponivel na regiao? | Service fica `Pending` |
| 10 | Taints e labels dos node pools (o cluster se chama `oke-gpu-prd`) | Pod `Pending` para sempre, ou GPU desperdicada com carga CPU-only |
| 11 | CNI: VCN-native pod networking ou flannel? | CIDR de origem nas regras de rede para o PostgreSQL |
| 12 | O time de rede permite `security-list-management-mode: All`? | Se nao, e obrigatorio `None` + NSG pre-provisionado |

## 2. Preencher, nesta ordem

1. **`kustomization.yaml` → `images[0].newName`**
   `<region-key>.ocir.io/<tenancy-namespace>/lukato` (respostas 1 e 2).
2. **`configMapGenerator`**: `CORS_ORIGINS`, `LANGFUSE_HOST`, `LLM__BASE_URL`,
   `EMBEDDING__BASE_URL`.
3. **Patch do Ingress**: host, host do TLS e `ingressClassName` (resposta 7).
   Se a resposta 7 for "nenhum", ignore o Ingress e descomente o bloco
   **ALTERNATIVA (a)** — Service `type=LoadBalancer`.
4. **Patch da NetworkPolicy**: CIDR da subnet do PostgreSQL gerenciado.
   Se o banco for no proprio cluster, remova esse patch (o `podSelector` do base
   ja resolve).
5. **Node placement** (resposta 10): descomente o bloco de `nodeSelector` se
   houver node pool CPU-only.
6. **Secret `lukato-secrets`**: nao esta neste overlay. Use
   `deploy/k8s/base/externalsecret.example.yaml` ou crie o Secret fora do Git.
7. **`imagePullSecret`**: o overlay ja referencia `ocirsecret` no Deployment e no
   Job de migracao. O Secret em si e criado no cluster, nao versionado:

   ```bash
   kubectl create secret docker-registry ocirsecret \
     --namespace=lukato \
     --docker-server=gru.ocir.io \
     --docker-username='<tenancy-namespace>/<dominio>/<usuario>' \
     --docker-password='<auth-token>' \
     --docker-email='<email>'
   ```

   A senha e um **Auth Token** do OCI (Console → perfil → Auth Tokens), nunca a
   senha do console.

## 3. Conferir antes de aplicar

```bash
# 1. renderiza sem erro?
kubectl kustomize deploy/k8s/overlays/oke > /tmp/lukato-oke.yaml

# 2. sobrou algum placeholder? (tem que voltar VAZIO)
grep -n 'PREENCHER' /tmp/lukato-oke.yaml

# 3. o servidor aceita? (nao cria nada)
kubectl apply -k deploy/k8s/overlays/oke --dry-run=server
```

O passo 2 e o mais importante: se `grep` retornar qualquer linha, **pare**.

## 4. Conferir que deu certo

```bash
kubectl -n lukato get job lukato-migrate -w         # migracao antes de tudo
kubectl -n lukato rollout status deploy/lukato --timeout=300s
kubectl -n lukato port-forward svc/lukato 8080:80
curl -s localhost:8080/healthz | jq
curl -s localhost:8080/readyz  | jq   # `tracer: degraded` sem Langfuse e ESPERADO
```

Detalhamento de cada verificacao, tabela de problemas e rollback:
[`docs/DEPLOY.md`](../../../../docs/DEPLOY.md).
