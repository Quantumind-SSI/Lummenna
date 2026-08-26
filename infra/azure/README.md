# Lumena on Azure — Infrastructure as Code

Bicep template provisioning the minimal honest footprint for running Lumena
on AKS: the cluster, a container registry, and the observability pair
(Log Analytics + Application Insights). No HA theater — Lumena is a
single-writer, single-replica workload.

## Provision

```bash
az group create -n rg-lummenna -l westeurope
az deployment group create -g rg-lummenna -f infra/azure/main.bicep \
  -p namePrefix=lummenna nodeVmSize=Standard_D4s_v5 nodeCount=2

# Capture outputs
ACR=$(az deployment group show -g rg-lummenna -n main --query properties.outputs.acrLoginServer.value -o tsv)
AKS=$(az deployment group show -g rg-lummenna -n main --query properties.outputs.aksClusterName.value -o tsv)
AI_CONN=$(az deployment group show -g rg-lummenna -n main --query properties.outputs.appInsightsConnectionString.value -o tsv)
az aks get-credentials -g rg-lummenna -n "$AKS"
```

## Build & push the image

```bash
az acr login -n "${ACR%%.*}"
docker build -t "$ACR/lummenna:1.0.0" .
docker push "$ACR/lummenna:1.0.0"
```

(Or let the `docker-publish` GitHub Actions workflow push to GHCR/ACR on tags.)

## Deploy the chart

```bash
kubectl create namespace lummenna
kubectl -n lummenna create secret generic lummenna-auth \
  --from-literal=api-key="$(openssl rand -hex 32)"

helm upgrade --install lummenna charts/lummenna -n lummenna \
  --set image.repository="$ACR/lummenna" \
  --set image.tag=1.0.0 \
  --set auth.existingSecret=lummenna-auth \
  --set model.url="https://<your-blob>/models/bge-small-en-v1.5-r5c38ec7.tar.gz" \
  --set model.sha256="<sha256 of the tarball>"
```

## Enable Application Insights export (optional, leaves sovereign mode)

Lumena refuses to export telemetry while `LUMENA_SOVEREIGN=true` — this is a
deliberate fail-loud gate, not a default integration.

```bash
kubectl -n lummenna create secret generic lummenna-appinsights \
  --from-literal=connection-string="$AI_CONN"

helm upgrade lummenna charts/lummenna -n lummenna --reuse-values \
  --set azureMonitor.enabled=true \
  --set azureMonitor.existingSecret=lummenna-appinsights \
  --set config.sovereign="false"
```

The image must include the azure extra (`pip install "lummenna[azure]"` in a
derived image) — startup fails with an actionable error otherwise.

## Metrics scraping

Pods carry `prometheus.io/*` annotations for `/metrics/prometheus`. With
Azure Monitor managed Prometheus, enable pod-annotation scraping in the
`ama-metrics-settings-configmap` (`podannotationnamespaceregex: "lummenna"`).

## GitHub Actions OIDC (no stored cloud credentials)

```bash
APP_ID=$(az ad app create --display-name lummenna-deployer --query appId -o tsv)
az ad sp create --id "$APP_ID"
az role assignment create --assignee "$APP_ID" --role "Azure Kubernetes Service Cluster User Role" \
  --scope $(az aks show -g rg-lummenna -n "$AKS" --query id -o tsv)
az role assignment create --assignee "$APP_ID" --role AcrPush \
  --scope $(az acr show -n "${ACR%%.*}" --query id -o tsv)
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "gh-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:QuantumindSSI/lummenna:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

Then set repository secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID` (IDs, not secrets — OIDC exchanges tokens at runtime)
and variables `AKS_RESOURCE_GROUP`, `AKS_CLUSTER_NAME`, `ACR_LOGIN_SERVER`
for the `deploy-aks` workflow.

## CI/CD pipeline overview

The GitHub Actions workflows form an end-to-end Azure pipeline:

| Stage | Workflow | Trigger | What it does |
|---|---|---|---|
| **CI** | `ci.yml` | PR / push to main | lint, typecheck, test matrix (3 OS × 3 py), build |
| **Security** | `security.yml` | PR / push / daily | secrets scan, dep audit, SAST, license check |
| **Infra** | `infra.yml` | PR on `infra/azure/**` (what-if); dispatch (apply) | provisions `main.bicep` via OIDC |
| **Image → GHCR + ACR** | `docker-publish.yml` | tag `v*` / dispatch | builds once, pushes identical tags to GHCR (public) **and** ACR (AKS pull source) |
| **Release** | `release.yml` | tag `v*.*.*` | sdist/wheel, GitHub release, PyPI (trusted publisher) |
| **Deploy** | `deploy-aks.yml` | tag `v*.*.*` (auto → staging); dispatch (staging or production) | Helm upgrade with model-aware readiness gate |

### Required repository configuration

**Secrets** (IDs, not credentials — OIDC exchanges tokens at runtime):
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

**Variables:**

| Variable | Used by | Example |
|---|---|---|
| `AZURE_RESOURCE_GROUP` | infra | `rg-lummenna` |
| `AZURE_LOCATION` | infra | `westeurope` |
| `ACR_LOGIN_SERVER` | image, deploy | `lumenaacr1234.azurecr.io` |
| `AKS_RESOURCE_GROUP` | deploy | `rg-lummenna` |
| `AKS_CLUSTER_NAME` | deploy | `lummenna-aks` |

### Two registries, one artifact

`docker-publish.yml` pushes the **same** semver + `sha-<short>` tags to both
GHCR (public distribution) and ACR (private, what AKS pulls). ACR uses
server-side `az acr build` so no registry password is stored, and the AKS
kubelet identity already holds `AcrPull` (granted in `main.bicep`).

### Staging → production promotion

`deploy-aks.yml` is progressive:

- A tag push (`v1.2.3`) **auto-deploys to staging** (namespace `lummenna-staging`).
- A manual run deploys a chosen tag to **staging or production** (namespace
  `lummenna`).

Production runs in the GitHub **`production` Environment**. Configure it with a
**required reviewer** (Settings → Environments → production) so promotion is a
human decision, not a merge side effect. Create a `staging` Environment too
(no gate). Both namespaces need their own `lummenna-auth` secret:

```bash
for ns in lummenna-staging lummenna; do
  kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f -
  kubectl -n "$ns" create secret generic lummenna-auth \
    --from-literal=api-key="$(openssl rand -hex 32)" \
    --dry-run=client -o yaml | kubectl apply -f -
done
```

### Provisioning via CI instead of the CLI

Rather than the manual `az deployment group create` above, run the **Infra**
workflow: open a PR touching `infra/azure/**` to get a `what-if` preview, then
trigger `infra.yml` via *Run workflow* with `confirm=APPLY`. The apply job
prints the ACR login server and AKS cluster name to the run summary so you can
set the deploy variables.
