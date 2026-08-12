# Lumena on Azure — Infrastructure as Code

Bicep template provisioning the minimal honest footprint for running Lumena
on AKS: the cluster, a container registry, and the observability pair
(Log Analytics + Application Insights). No HA theater — Lumena is a
single-writer, single-replica workload.

## Provision

```bash
az group create -n rg-lumena -l westeurope
az deployment group create -g rg-lumena -f infra/azure/main.bicep \
  -p namePrefix=lumena nodeVmSize=Standard_D4s_v5 nodeCount=2

# Capture outputs
ACR=$(az deployment group show -g rg-lumena -n main --query properties.outputs.acrLoginServer.value -o tsv)
AKS=$(az deployment group show -g rg-lumena -n main --query properties.outputs.aksClusterName.value -o tsv)
AI_CONN=$(az deployment group show -g rg-lumena -n main --query properties.outputs.appInsightsConnectionString.value -o tsv)
az aks get-credentials -g rg-lumena -n "$AKS"
```

## Build & push the image

```bash
az acr login -n "${ACR%%.*}"
docker build -t "$ACR/lumena:1.0.0" .
docker push "$ACR/lumena:1.0.0"
```

(Or let the `docker-publish` GitHub Actions workflow push to GHCR/ACR on tags.)

## Deploy the chart

```bash
kubectl create namespace lumena
kubectl -n lumena create secret generic lumena-auth \
  --from-literal=api-key="$(openssl rand -hex 32)"

helm upgrade --install lumena charts/lumena -n lumena \
  --set image.repository="$ACR/lumena" \
  --set image.tag=1.0.0 \
  --set auth.existingSecret=lumena-auth \
  --set model.url="https://<your-blob>/models/bge-small-en-v1.5-r5c38ec7.tar.gz" \
  --set model.sha256="<sha256 of the tarball>"
```

## Enable Application Insights export (optional, leaves sovereign mode)

Lumena refuses to export telemetry while `LUMENA_SOVEREIGN=true` — this is a
deliberate fail-loud gate, not a default integration.

```bash
kubectl -n lumena create secret generic lumena-appinsights \
  --from-literal=connection-string="$AI_CONN"

helm upgrade lumena charts/lumena -n lumena --reuse-values \
  --set azureMonitor.enabled=true \
  --set azureMonitor.existingSecret=lumena-appinsights \
  --set config.sovereign="false"
```

The image must include the azure extra (`pip install "lumena[azure]"` in a
derived image) — startup fails with an actionable error otherwise.

## Metrics scraping

Pods carry `prometheus.io/*` annotations for `/metrics/prometheus`. With
Azure Monitor managed Prometheus, enable pod-annotation scraping in the
`ama-metrics-settings-configmap` (`podannotationnamespaceregex: "lumena"`).

## GitHub Actions OIDC (no stored cloud credentials)

```bash
APP_ID=$(az ad app create --display-name lumena-deployer --query appId -o tsv)
az ad sp create --id "$APP_ID"
az role assignment create --assignee "$APP_ID" --role "Azure Kubernetes Service Cluster User Role" \
  --scope $(az aks show -g rg-lumena -n "$AKS" --query id -o tsv)
az role assignment create --assignee "$APP_ID" --role AcrPush \
  --scope $(az acr show -n "${ACR%%.*}" --query id -o tsv)
az ad app federated-credential create --id "$APP_ID" --parameters '{
  "name": "gh-main",
  "issuer": "https://token.actions.githubusercontent.com",
  "subject": "repo:QuantumindSSI/lumena:ref:refs/heads/main",
  "audiences": ["api://AzureADTokenExchange"]
}'
```

Then set repository secrets `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID` (IDs, not secrets — OIDC exchanges tokens at runtime)
and variables `AKS_RESOURCE_GROUP`, `AKS_CLUSTER_NAME`, `ACR_LOGIN_SERVER`
for the `deploy-aks` workflow.
