# Runbook de implantacao — lukato 1.0.0 em OKE

Ordem de execucao real, do zero ate a aplicacao respondendo. Cada passo tem um
criterio de sucesso; se ele nao bater, **pare** e va para a secao
[Solucao de problemas](#7-solucao-de-problemas).

Alvo: Oracle Kubernetes Engine (OKE). O overlay fica em
`deploy/k8s/overlays/oke/` — leia o [README dele](../deploy/k8s/overlays/oke/README.md)
para a lista de preenchimento.

> Este runbook foi escrito a partir de pesquisa em documentacao publica da Oracle
> e do codigo-fonte dos controllers. Nenhum passo foi executado contra a tenancy
> da Claro. Onde a pesquisa nao confirmou algo, esta escrito **A CONFIRMAR** —
> trate como pergunta ao time de plataforma, nao como detalhe.

---

## 1. Pre-requisitos e decisoes

### Ferramentas

| Ferramenta | Para que |
|---|---|
| `docker` (ou `podman`) | construir e publicar a imagem |
| `kubectl` >= 1.27 com `kustomize` embutido | renderizar e aplicar |
| `psql` | criar extensoes no banco |
| Acesso ao Console OCI | gerar Auth Token, criar repo OCIR, ver o endpoint do banco |

### Quatro decisoes antes de tocar em qualquer arquivo

**1) Banco: gerenciado ou dentro do cluster?**

- **OCI Database with PostgreSQL** (recomendado em prod). Endpoint FQDN privado,
  porta 5432, alcancavel so de dentro da VCN. **TLS e obrigatorio** — conexao
  nao-SSL nao e suportada.
  *Consequencias:* a NetworkPolicy precisa de `ipBlock` com o CIDR da subnet do
  banco (o overlay OKE ja tem esse patch), o CA cert precisa chegar ao pod se a
  politica exigir `verify-ca`/`verify-full`, e voce provavelmente **nao** tera
  superusuario para `CREATE EXTENSION` (ver secao 3).
- **PostgreSQL no cluster** (`pgvector/pgvector:pg16`, como no `docker-compose.yml`).
  Mais simples de subir, mas voce assume backup, HA e upgrade. Nesse caso remova
  o patch da NetworkPolicy no overlay: o `podSelector` do base ja cobre.

**A CONFIRMAR:** a politica interna exige `sslmode=verify-full` ou aceita
`require`? E o CA do banco e rotacionado? Isso decide se o CA vai num ConfigMap
estatico ou precisa vir do Vault.

**2) Ingress: publico ou privado? E qual controller?**

Tres caminhos validos em OKE, nenhum universalmente correto:

| Caminho | Quando |
|---|---|
| (a) `Service type=LoadBalancer` + annotations OCI | nao ha ingress padronizado no cluster; menor superficie para 1 servico HTTP |
| (b) OCI Native Ingress Controller (add-on) | ja adotado; 1 LB para varias rotas |
| (c) nginx-ingress | ja adotado; manifesto portavel entre clusters |

O `base/` assume (c). **Se a Claro ja padroniza um ingress, use o que existe** —
nao introduza um terceiro.

Atencao: `service.beta.kubernetes.io/oci-load-balancer-internal` e **imutavel
apos a criacao**. Errar publico/privado exige recriar o Service.

**3) De onde vem o segredo?**

| Opcao | Observacao |
|---|---|
| External Secrets Operator + OCI Vault | molde pronto em `deploy/k8s/base/externalsecret.example.yaml`. **A CONFIRMAR:** nao confirmei que o ESO exista como add-on gerenciado do OKE — assuma Helm |
| OCI Secrets Store CSI Driver Provider | monta segredos como **arquivos**; o lukato le tudo de variavel de ambiente, entao encaixa pior |
| `kubectl create secret` manual | aceitavel para o primeiro deploy; anote quem rotaciona |

`principalType: Workload` (workload identity, sem chave estatica) **so funciona em
enhanced cluster**. Descobrir isso no meio do deploy e caro — pergunte antes.

**4) Onde o pod roda?**

O cluster se chama `oke-gpu-prd`. Se os nodes GPU tiverem taint, o lukato
(CPU-only) fica `Pending` para sempre; se nao tiverem, ele pode ocupar uma GPU
por acidente. Confirme taints/labels e preencha `nodeSelector`/`tolerations` no
overlay.

---

## 2. Construir e publicar a imagem

Preencha antes:

```bash
export REGION_KEY=gru                     # gru = sa-saopaulo-1 | vcp = sa-vinhedo-1
export TENANCY_NS='<tenancy-namespace>'   # Object Storage namespace, ex. ansh81vru1zp
export OCIR="${REGION_KEY}.ocir.io/${TENANCY_NS}"
export IMAGE="${OCIR}/lukato:1.0.0"
```

> `TENANCY_NS` **nao** e o nome da tenancy: e o Object Storage namespace, uma
> string alfanumerica autogerada e imutavel. Console → Tenancy details.

### 2.1 Build

```bash
cd /caminho/do/lukato
docker build -t lukato:1.0.0 .
```

Se a rede corporativa bloquear o CDN de blobs do Docker Hub, aponte para o mirror
interno:

```bash
docker build --build-arg PYTHON_IMAGE=<registry-interno>/python:3.11-slim-bookworm -t lukato:1.0.0 .
```

Fumaça local (opcional, mas barato):

```bash
docker run --rm -p 8000:8000 -e LUKATO_DB__AUTO_FALLBACK=true lukato:1.0.0 &
curl -fsS localhost:8000/healthz
```

### 2.2 Login no OCIR

```bash
docker login ${REGION_KEY}.ocir.io
# Username: <tenancy-namespace>/<usuario>
#   ou, se a tenancy usar identity domains / federacao IDCS:
#            <tenancy-namespace>/<dominio>/<usuario>
# Password: Auth Token do OCI (Console → perfil → Auth Tokens → Generate Token)
```

O Auth Token **nao** e a senha do console, e so e exibido uma vez.
Formato errado do username e a causa numero 1 de `unauthorized` aqui.

### 2.3 Tag e push

```bash
docker tag lukato:1.0.0 "${IMAGE}"
docker push "${IMAGE}"
```

O push so cria o repositorio automaticamente se a tenancy tiver
*"Create repository on first push in root compartment"* habilitado **e** o usuario
tiver `REPOSITORY_MANAGE`. Em ambiente corporativo isso costuma estar desligado —
peca o repo pre-criado no compartment correto.

**Criterio de sucesso:** `docker push` termina sem erro e o repositorio aparece no
Console em Developer Services → Container Registry.

### 2.4 imagePullSecret no cluster

```bash
kubectl create namespace lukato --dry-run=client -o yaml | kubectl apply -f -

kubectl create secret docker-registry ocirsecret \
  --namespace=lukato \
  --docker-server="${REGION_KEY}.ocir.io" \
  --docker-username="${TENANCY_NS}/<dominio>/<usuario>" \
  --docker-password='<auth-token>' \
  --docker-email='<email>'
```

E um Secret `kubernetes.io/dockerconfigjson` comum — nao existe tipo proprietario
da Oracle. O overlay ja referencia o nome `ocirsecret` no Deployment e no Job.

---

## 3. Banco

### 3.1 Criar o banco e o usuario

Com **OCI Database with PostgreSQL**: crie o DB system, anote o **FQDN privado** e
o CA cert em *Connection details*. Depois crie o banco da aplicacao:

```sql
CREATE DATABASE lukato;
CREATE USER lukato_app WITH PASSWORD '<gerada, nunca reaproveitada>';
GRANT ALL PRIVILEGES ON DATABASE lukato TO lukato_app;
```

Libere 5432 nas security lists / NSGs, da subnet dos pods para a subnet do banco.
Com **VCN-native pod networking** a origem do trafego e o **IP do pod**, nao o do
node — a regra tem que cobrir o CIDR certo.

### 3.2 Habilitar pgvector — o ponto que costuma travar

As migracoes executam `CREATE EXTENSION IF NOT EXISTS vector` (na `0001`, **antes**
das tabelas, porque `chunks` e `ad_fingerprints` declaram colunas `VECTOR(1024)`) e
`CREATE EXTENSION IF NOT EXISTS pg_trgm` (na `0002`).

Em banco gerenciado o usuario da aplicacao normalmente **nao** e superusuario e
`CREATE EXTENSION` falha com `permission denied to create extension "vector"`.

**Solucao:** peca ao DBA / time de plataforma que crie as extensoes **uma vez**,
com um usuario que tenha permissao:

```sql
\c lukato
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

Feito isso, o `IF NOT EXISTS` das migracoes vira no-op e **nao** exige permissao —
o Job passa com o usuario comum. Nao ha nada a mudar no codigo.

**A CONFIRMAR:** OCI Database with PostgreSQL expoe `pgvector` na lista de
extensoes permitidas? Se nao expuser, a decisao "banco gerenciado ou no cluster"
(secao 1) muda de resposta — e melhor descobrir isso agora do que no dia do go-live.

Conferencia:

```sql
SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','pg_trgm');
```

### 3.3 A URL do banco e a pegadinha do `sslmode`

A URL vai no Secret, chave `LUKATO_DB__URL`:

```
postgresql+asyncpg://lukato_app:<senha>@<fqdn-privado>:5432/lukato
```

**Nao acrescente `?sslmode=require` na URL.** O `asyncpg` so entende `sslmode`
quando ele vem dentro de uma DSN literal; quando o SQLAlchemy converte a query
string em kwargs, o resultado e:

```
TypeError: connect() got an unexpected keyword argument 'sslmode'
```

O pod entra em `CrashLoopBackOff` no boot. Para `verify-full` o caminho e um
`ssl.SSLContext` via `connect_args`, com o CA do banco montado no pod:

```python
import ssl
ctx = ssl.create_default_context(cafile="/etc/pg/ca.pem")
engine = create_async_engine(URL_SEM_SSLMODE, connect_args={"ssl": ctx})
```

Isso e mudanca de codigo/composicao, nao de manifesto — se a politica exigir
`verify-full`, abra a tarefa antes do deploy.

### 3.4 Rodar as migracoes

O `Job` `lukato-migrate` executa `entrypoint.sh migrate` (`alembic upgrade head`) e
ja esta no base. O `serve` **nao** migra: `LUKATO_DB__RUN_MIGRATIONS=false` no
ConfigMap, de proposito — migracao concorrente entre replicas e uma forma criativa
de corromper schema.

O Job e criado junto com o `apply` da secao 5. Para roda-lo isoladamente, antes do
resto (recomendado no primeiro deploy):

```bash
kubectl kustomize deploy/k8s/overlays/oke | \
  kubectl apply -f - --selector app.kubernetes.io/component=migration

kubectl -n lukato wait --for=condition=complete job/lukato-migrate --timeout=300s
kubectl -n lukato logs job/lukato-migrate
```

**Criterio de sucesso:** o Job termina `Complete` e o log mostra as duas revisoes
(`0001_esquema_inicial`, `0002_indices_pgvector`).

> Jobs sao **imutaveis**. Reaplicar o overlay depois de mudar a imagem falha com
> `field is immutable`. Antes de reaplicar: `kubectl -n lukato delete job lukato-migrate`.

---

## 4. Segredos

Seis chaves, todas no Secret `lukato-secrets`, consumidas por `envFrom`:

| Chave | Como obter |
|---|---|
| `LUKATO_DB__URL` | montada na secao 3.3 |
| `LUKATO_LLM__API_KEY` | virtual key do hub GPU — CCOE Arquitetura (GPU Hub) |
| `LUKATO_EMBEDDING__API_KEY` | chave do endpoint de embeddings (se exigido) |
| `LUKATO_SECURITY__JWT_SECRET` | `openssl rand -hex 32` — **minimo 32 caracteres** |
| `LUKATO_OBSERVABILITY__LANGFUSE_PUBLIC_KEY` | projeto no Langfuse (`pk-lf-...`) |
| `LUKATO_OBSERVABILITY__LANGFUSE_SECRET_KEY` | projeto no Langfuse (`sk-lf-...`) |

Caminho recomendado: `deploy/k8s/base/externalsecret.example.yaml` (ESO + OCI
Vault). Caminho manual, aceitavel no primeiro deploy:

```bash
kubectl -n lukato create secret generic lukato-secrets \
  --from-literal=LUKATO_DB__URL='postgresql+asyncpg://lukato_app:...@...:5432/lukato' \
  --from-literal=LUKATO_LLM__API_KEY='...' \
  --from-literal=LUKATO_EMBEDDING__API_KEY='...' \
  --from-literal=LUKATO_SECURITY__JWT_SECRET="$(openssl rand -hex 32)" \
  --from-literal=LUKATO_OBSERVABILITY__LANGFUSE_PUBLIC_KEY='...' \
  --from-literal=LUKATO_OBSERVABILITY__LANGFUSE_SECRET_KEY='...'
```

Se o Langfuse ainda nao existir, deixe
`LUKATO_OBSERVABILITY__LANGFUSE_ENABLED=false` no overlay e ponha string vazia nas
duas chaves — o tracer vira no-op e `/readyz` reporta `tracer: degraded`, o que e
esperado e **nao** derruba a readiness.

### O que NUNCA versionar

- Nenhum valor real em `secret.example.yaml`, em ConfigMap, em `.env` commitado,
  em `Dockerfile`, em log ou em issue. O CI falha se achar algo com a forma de
  `sk-…`, `AKIA…` ou chave privada PEM.
- Auth Token do OCIR: vive no Secret `ocirsecret`, criado por comando, nunca em
  arquivo do repositorio.
- Rotacionar segredo **nao** reinicia os pods: `envFrom` so e lido no boot. Depois
  de rotacionar, `kubectl -n lukato rollout restart deploy/lukato`.

---

## 5. Aplicar

```bash
# 1. renderiza?
kubectl kustomize deploy/k8s/overlays/oke > /tmp/lukato-oke.yaml

# 2. sobrou placeholder? TEM QUE VOLTAR VAZIO
grep -n 'PREENCHER' /tmp/lukato-oke.yaml

# 3. o servidor aceita?
kubectl apply -k deploy/k8s/overlays/oke --dry-run=server

# 4. aplicar
kubectl apply -k deploy/k8s/overlays/oke
```

O namespace `lukato` vem com labels de Pod Security Admission
`enforce=restricted`. O pod do lukato ja atende (`runAsNonRoot`,
`allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`,
`seccompProfile: RuntimeDefault`, `readOnlyRootFilesystem: true`). Se o namespace
ja existir com outras labels, o `apply` **nao** as sobrescreve silenciosamente —
confira com `kubectl get ns lukato --show-labels`.

---

## 6. Validar

```bash
# migracao primeiro
kubectl -n lukato get job lukato-migrate
kubectl -n lukato logs job/lukato-migrate

# rollout
kubectl -n lukato rollout status deploy/lukato --timeout=300s
kubectl -n lukato get pods -l app.kubernetes.io/name=lukato
```

Sonda direta, sem depender do ingress:

```bash
kubectl -n lukato port-forward svc/lukato 8080:80
```

| Verificacao | Esperado |
|---|---|
| `curl -s localhost:8080/healthz` | `200`. E uma **constante**: nao toca em dependencia nenhuma. `/healthz` verde nao significa que o banco esta de pe |
| `curl -s localhost:8080/readyz \| jq` | `200` com `database: ok`. **`503` acontece somente com o banco fora** |
| campo `tracer` em `/readyz` | `degraded` sem Langfuse configurado — **esperado**, e o tracer no-op. Nao investigue |
| campos `llm` / `embeddings` | `degraded` se o hub nao responder. Nao derruba readiness, mas **e sintoma**: sem embeddings a ingestao de conhecimento nao funciona |
| `curl -s localhost:8080/api/docs` | Swagger UI (OpenAPI em `/api/openapi.json`, ReDoc em `/api/redoc`) |
| `curl -s localhost:8080/metrics` | exposicao Prometheus |

### Seed (opcional, idempotente)

Popula prompts, guardrails, modulos de demonstracao e um usuario root. Rodar duas
vezes nao duplica nada.

A forma mais simples e reaproveitar um pod que ja esta de pe com o ConfigMap e o
Secret montados:

```bash
kubectl -n lukato exec deploy/lukato -- /usr/local/bin/entrypoint.sh seed
```

Se a senha do root nao vier em `LUKATO_SEED_ROOT_PASSWORD`, o seed **sorteia uma
e a imprime uma unica vez**. Capture a saida. E-mail padrao:
`root@lukato.local` (mude com `LUKATO_SEED_ROOT_EMAIL`).

Em producao com auth ligada, considere pular o seed e criar o root pela CLI com
credenciais controladas — os modulos de demonstracao nao pertencem a um ambiente
produtivo.

### Pelo ingress

```bash
curl -fsS https://<fqdn>/healthz
curl -fsS https://<fqdn>/api/docs -o /dev/null -w '%{http_code}\n'
```

---

## 7. Solucao de problemas

| Sintoma | Causa provavel | Acao |
|---|---|---|
| `ImagePullBackOff` / `ErrImagePull` | (1) `newName` da imagem errado — tenancy namespace nao e o nome da tenancy; (2) `ocirsecret` ausente ou no namespace errado; (3) username do OCIR sem o `<dominio>` numa tenancy federada; (4) repo OCIR nao existe | `kubectl -n lukato describe pod <pod>` e leia a mensagem do registry. Refaca `docker login` com o formato exato e recrie o `ocirsecret`. Confirme que o `imagePullSecrets` esta tambem no **Job** de migracao |
| `CrashLoopBackOff`, log com `ValidationError` / `field required` de `LUKATO_*` | Secret `lukato-secrets` ausente ou sem uma das 6 chaves | `kubectl -n lukato get secret lukato-secrets -o jsonpath='{.data}' \| jq keys`. Se estiver usando ESO: `kubectl -n lukato describe externalsecret lukato-secrets` |
| Boot recusado: *"AUTH_ENABLED=false em producao expoe a API inteira como root anonimo"* | `LUKATO_SECURITY__AUTH_ENABLED=false` com `LUKATO_APP__ENV=prod` | **Nao desligue a validacao.** Ligue a auth (`=true`) ou pare de declarar o ambiente como `prod`. Com auth desligada em prod, toda rota responde sem credencial |
| Boot recusado: *"JWT_SECRET tem N caracteres; em producao o minimo e 32"* | segredo curto, ou o valor de exemplo (`troque-este-segredo-em-producao`) veio junto | `openssl rand -hex 32`, grave no cofre, atualize o Secret e `rollout restart`. HMAC curto e quebravel por forca bruta |
| Boot recusado: *"GUARDRAILS__ENABLED=false em producao desliga a trinca..."* | guardrails desligados globalmente | Para afrouxar **um** modulo, troque a politica dele. A chave geral e so para diagnostico local |
| Job de migracao falha com `type "vector" does not exist` | extensao `vector` nao existe **e** o usuario nao pode cria-la | Peca ao DBA: `CREATE EXTENSION IF NOT EXISTS vector;` no banco `lukato` (secao 3.2). Depois `kubectl -n lukato delete job lukato-migrate` e reaplique |
| Job falha com `permission denied to create extension` | mesma causa, mensagem antes de chegar no `CREATE TABLE` | idem |
| `/readyz` responde `503`, `database: down` | banco inalcancavel: NetworkPolicy sem `ipBlock` do CIDR do banco; security list/NSG sem 5432; FQDN errado; com VCN-native pod networking a regra usa o CIDR do **node** em vez do **pod** | `kubectl -n lukato exec deploy/lukato -- python -c "import socket;print(socket.gethostbyname('<fqdn>'))"`; revise o patch da NetworkPolicy no overlay e as regras da VCN |
| `CrashLoopBackOff` com `TypeError: connect() got an unexpected keyword argument 'sslmode'` | `?sslmode=...` na `LUKATO_DB__URL` | Remova a query string da URL. TLS via `SSLContext` em `connect_args` (secao 3.3) |
| Busca semantica devolve resultado sem sentido | alguem usou `LUKATO_EMBEDDING__PROVIDER=hashing` para destravar o boot. Hashing e Qwen3 geram vetores de 1024 dimensoes em **espacos semanticos diferentes**; misturados na mesma colecao, a busca quebra em silencio e **de forma permanente** | `curl /api/v1/knowledge/health` → `degraded: true` e `reason` diz se e hashing ou provedor fora. Correcao: voltar para `provider=qwen` **e re-embeddar a colecao inteira** (`POST /api/v1/knowledge/documents/{id}/reindex`). Nao ha conserto parcial |
| Pods `Pending` sem evento de recurso | taint de GPU no node pool e sem toleration (cluster `oke-gpu-prd`) | `kubectl describe node <node> \| grep -i taint`; preencha `nodeSelector`/`tolerations` no overlay |
| Pods `Pending`, evento de IP | exaustao de IP com VCN-native pod networking (cada pod consome um IP secundario da subnet) | Fale com o time de rede. Isso trava tambem **upgrades** de node pool |
| Pods sem rede nenhuma entre si | known issue: shape **bare metal** + VCN-native pod networking | node pool tem que usar shape VM |
| Service `type=LoadBalancer` fica `<pending>` para sempre | quota regional de LB esgotada, ou subnet/NSG errados nas annotations | Console → Limits, Quotas and Usage. Verifique `oci-load-balancer-subnet1` |
| NLB criado mas sem trafego | NLB **nao** cria security rules automaticamente | escrever as regras de ingresso/egresso a mao |
| Mudanca no Service nao surte efeito no LB, sem erro | *delete protection* habilitada no LB pelo Console: o CCM para de **atualizar** o recurso | desabilitar delete protection no LB |
| `apply` falha com `Job.batch ... field is immutable` | Job de migracao ja existe com outra imagem | `kubectl -n lukato delete job lukato-migrate` e reaplique |
| Pod recusado na admissao (`violates PodSecurity "restricted"`) | patch local afrouxou o `securityContext`, ou ha Kyverno/Gatekeeper com regra adicional | `kubectl get ns lukato --show-labels`; o base ja e compativel com `restricted` — reverta o patch |
| Node em `DiskPressure`, pods sendo evictados | os dois `emptyDir` (`/tmp`, `/app/var`) consomem o **boot volume** do node. Boot volume ampliado sem `oci-growfs` nao vira `Allocatable: ephemeral-storage` | o overlay ja declara `requests/limits` de `ephemeral-storage`; se persistir, e o node — estender a particao raiz |

---

## 8. Rollback

O caminho rapido, quando a versao nova sobe mas se comporta mal:

```bash
kubectl -n lukato rollout undo deploy/lukato
kubectl -n lukato rollout status deploy/lukato --timeout=300s
```

Para uma revisao especifica:

```bash
kubectl -n lukato rollout history deploy/lukato
kubectl -n lukato rollout undo deploy/lukato --to-revision=<N>
```

O Deployment guarda `revisionHistoryLimit: 5` e usa `maxUnavailable: 0`, entao o
rollback nao derruba a capacidade atual antes de subir a anterior.

### O que o rollback **nao** desfaz

- **Migracoes de banco.** `alembic upgrade head` ja rodou. Se a versao anterior
  nao for compativel com o schema novo, o rollback do Deployment nao basta:
  e preciso um `alembic downgrade` deliberado, com backup antes.
  Faca backup do banco **antes** de rodar o Job de migracao, sempre.
- **Embeddings gravados.** Vetor errado nao volta por rollback de imagem — exige
  re-embeddar a colecao.
- **Secrets rotacionados.** Voltar a imagem nao volta o valor do cofre.

Se o problema for de configuracao (nao de imagem), prefira corrigir o overlay e
reaplicar a fazer `rollout undo` — o `undo` volta o pod template inteiro, inclusive
o que estava certo.

---

## 9. O que este projeto NAO valida por voce

Isto nao e ressalva formal. Sao dois pontos que ninguem exercitou, e quem implanta
sera **a primeira pessoa** a exercita-los:

**1. O hub Qwen nunca foi exercitado de verdade.**
`https://hub-gpus.usto.re/v1` (LLM) e `https://hub-gpus.claro.com.br/embed06b/v1`
(embeddings) sao hosts internos, inalcancaveis do ambiente onde este projeto foi
construido. Toda a suite de testes roda com o adaptador `echo` (LLM) e com o
embedder `hashing`. Consequencias praticas:

- O formato de resposta real do hub nunca foi confrontado com o parser. Se o hub
  divergir da API da OpenAI em algum campo, o erro aparece no seu deploy.
- A autenticacao da virtual key nunca foi testada contra o endpoint real.
- `LUKATO_EMBEDDING__DIMENSIONS=1024` foi tomado da especificacao do
  `Qwen/Qwen3-Embedding-0.6B`, nao medido. **Confira a dimensao real na primeira
  chamada, antes de ingerir qualquer documento** — trocar `DIMENSIONS` depois
  obriga a re-embeddar a colecao inteira.
- Faca uma chamada de fumaça ao hub antes de liberar trafego, de dentro de um pod
  do namespace (a NetworkPolicy so libera 443 de saida).

**2. `docker compose up` nunca rodou no ambiente de construcao.**
O `docker-compose.yml` e o `Dockerfile` foram escritos e revisados, e o CI
constroi a imagem — mas a stack completa (Postgres + pgvector + API + console,
mais o perfil `obs` com Langfuse) nunca subiu de ponta a ponta aqui. O
`deploy/postgres/init/01-extensions.sql` so roda em volume novo: se voce ja tem um
volume `pgdata`, as extensoes **nao** serao criadas e a migracao falha com
`type "vector" does not exist` — mesmo sintoma da secao 7, causa diferente.

Consequencia: reserve tempo para o primeiro `up` e para a primeira chamada ao hub.
Nao os agende para a janela de go-live.
