# Lumena Production Deployment Guide

**Version:** v1.0.0  
**Platforms:** Raspberry Pi 5, Jetson Orin Nano, Generic x86_64, ARM64 SBCs

---

## 1. Pre-Deployment Checklist

### 1.1 Hardware Requirements

| Platform | RAM | Storage | OS | Network |
|---|---|---|---|---|
| Raspberry Pi 5 | 4 GB | 32 GB SD / NVMe | Raspberry Pi OS 64-bit | Optional LAN |
| Jetson Orin Nano | 8 GB | 128 GB NVMe | JetPack 6.x | Optional LAN |
| Generic x86_64 | 8 GB | 50 GB SSD | Ubuntu 22.04+ / Debian 12+ | Optional LAN |
| Orange Pi 5 | 8 GB | 64 GB eMMC | Armbian / Ubuntu | Optional LAN |

### 1.2 Environment Prerequisites

```bash
# Python 3.10+
python3 --version

# pip and venv
python3 -m pip --version
python3 -m venv --help

# SQLite with WAL support (default on most systems)
python3 -c "import sqlite3; print(sqlite3.sqlite_version)"  # >= 3.35.0

# Basic build tools (for llama-cpp-python and numba)
sudo apt-get update
sudo apt-get install -y build-essential cmake python3-dev

# (Optional) Disable swap on SD/eMMC to reduce flash wear
sudo dphys-swapfile swapoff
sudo systemctl disable dphys-swapfile
```

---

## 2. Installation

### 2.1 Standard Installation (Recommended)

```bash
# Create a dedicated virtual environment
python3 -m venv ~/.venvs/lummenna
source ~/.venvs/lummenna/bin/activate

# Install Lumena from PyPI (when published)
pip install lummenna

# Or install from source
git clone https://github.com/QuantmindSSI/lummenna.git
cd lummenna
pip install -e ".[dev]"
```

### 2.2 Edge-Optimized Installation

```bash
# For Raspberry Pi 5 (low-RAM, flash-wear conscious)
pip install lummenna \
  --no-binary :all: \
  --prefer-binary onnxruntime

# Download spaCy model for NER
python -m spacy download en_core_web_sm

# Optional: local LLM support for sleep-phase consolidation
pip install llama-cpp-python
```

### 2.3 Docker Deployment

```bash
# Build
docker build -t lummenna:latest .

# Run (production)
docker run -d \
  --name lummenna \
  -p 8848:8848 \
  -v ~/.lumena:/root/.lumena \
  -e LUMENA_DEVICE=generic \
  -e LUMENA_LOG_LEVEL=info \
  --restart unless-stopped \
  lummenna:latest

# Or use docker-compose
docker compose up -d
```

**docker-compose.yml** (provided in repo):
```yaml
version: "3.8"
services:
  lummenna:
    build: .
    ports:
      - "8848:8848"
    volumes:
      - ~/.lumena:/root/.lumena
    environment:
      - LUMENA_DEVICE=generic
      - LUMENA_LOG_LEVEL=info
      - LUMENA_API_HOST=0.0.0.0
      - LUMENA_API_PORT=8848
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8848/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
```

---

## 3. Configuration

### 3.1 Device Profiles

Create `~/.lumena/config.toml`:

```toml
# Raspberry Pi 5
[device.rpi5]
context_budget = 2048
memory_limit_mb = 300
embedding_model = "bge-small-en-v1.5"
embedding_dims = 384
vector_index = "sqlite-vec"
consolidation_cpu_percent = 5
scheduler_granularity = 300

# Jetson Orin Nano
[device.jetson-orin]
context_budget = 4096
memory_limit_mb = 800
vector_index = "usearch"
enable_frqad = true
enable_local_llm = true

# Generic x86 / server
[device.generic]
context_budget = 4096
memory_limit_mb = 1024
vector_index = "usearch"
enable_frqad = true
```

### 3.2 Environment Variables

```bash
# Required
export LUMENA_DEVICE="generic"          # rpi5 | jetson-orin | orange-pi | generic
export LUMENA_CONTEXT_BUDGET="4096"     # tokens
export LUMENA_MEMORY_LIMIT="500mb"      # RAM cap

# Twin-Force Controller defaults
export LUMENA_TFC_E="0.5"               # conservation bias (0-1)
export LUMENA_TFC_A="0.5"               # attentional temperature (0-1)
export LUMENA_TFC_TAU="7d"              # temporal horizon
export LUMENA_TFC_R="3"                 # resolution level (0-5)

# Sovereign mode
export LUMENA_SOVEREIGN="true"          # block all external API calls
export LUMENA_LOG_LEVEL="info"          # debug | info | warning | error
```

### 3.3 SQLite Tuning for Production

```bash
sqlite3 ~/.lumena/store/lumena.db <<EOF
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA mmap_size=268435456;  -- 256 MB
PRAGMA temp_store=MEMORY;
PRAGMA cache_size=-32768;    -- 32 MB page cache
EOF
```

---

## 4. Health Checks & Monitoring

### 4.1 API Status Endpoint

```bash
# Default status
curl http://localhost:8848/v1/status

# Expected response:
# {"rooms": 12, "active_chunks": 2341, "tfc": {"e": 0.62, "a": 0.44, "tau": 7.0, "r": 3}, "embedding_model_available": true}

# Liveness probe
curl http://localhost:8848/health
# {"status": "ok"}

# Readiness probe (model-aware): 503 until the embedding model is loaded
curl http://localhost:8848/ready
# {"status": "ready", "embedding_model_available": true, "scheduler_running": true}

# Prometheus exposition (request counters, latency histograms, palace gauges)
curl http://localhost:8848/metrics/prometheus
```

### 4.2 CLI Status

```bash
lummenna status

# Output:
# Lumena Status
# Device: generic
# Rooms: 12
# Active chunks: 2341
# Context budget: 2048 tokens
# TFC → e=0.62 a=0.44 tau=7.0 r=3
```
```

### 4.3 Log Monitoring

```bash
# Structured JSON logs
tail -f ~/.lumena/logs/audit.jsonl | jq .

# Log format:
# {"timestamp":"2026-07-20T12:00:00Z","component":"LUMENA:FORCE:MNEMONIC",
#  "level":"info","event":"CONSOLIDATE","room":"preferences","new_chunks":12}
```

### 4.4 Azure Monitor / Application Insights (opt-in)

Lumena can export OpenTelemetry traces and logs to Application Insights.
This is **sovereignty-gated and fail-loud**:

- Set `APPLICATIONINSIGHTS_CONNECTION_STRING` (standard Azure convention).
- Set `LUMENA_SOVEREIGN=false` — export while sovereign mode is on raises
  `SovereignViolationError` at startup instead of silently leaking traces.
- Install the extra: `pip install "lummenna[azure]"`. If the connection string
  is set but the package is missing, startup fails with an actionable error —
  Lumena never runs without telemetry the operator configured.

Memory content is never attached to spans by Lumena's instrumentation.

For metrics-only monitoring without leaving sovereign mode, scrape
`/metrics/prometheus` with any Prometheus-compatible agent (including Azure
Monitor managed Prometheus via pod annotations) — scraping is inbound and
does not violate the sovereign boundary.

---

## 4b. Kubernetes / AKS Deployment

A Helm chart ships at `charts/lummenna/` and Azure IaC at `infra/azure/`
(Bicep: AKS + ACR + Log Analytics + Application Insights). Full walkthrough:
`infra/azure/README.md`. CI/CD: `.github/workflows/docker-publish.yml`
(image build/push + smoke test) and `.github/workflows/deploy-aks.yml`
(manual Helm deploy via OIDC — no stored cloud credentials).

**Honest constraints — read first:**

| Constraint | Why |
|---|---|
| `replicaCount` is 1 and the chart **refuses** more | SQLite is single-writer, single-file; extra replicas would each hold an independent, divergent palace |
| Rolling updates briefly stop the writer | One writer per store PVC; expect seconds of unavailability per upgrade — this is not an HA system |
| Pod is NotReady until a model is provisioned | `/ready` gates on the embedding model; no silent lexical-only degradation behind a Service |
| Model artifacts are checksum-pinned | The init container refuses an artifact without a `sha256`; models are versioned data, not image layers |

```bash
# Quickstart (after infra/azure provisioning)
kubectl create namespace lummenna
kubectl -n lummenna create secret generic lummenna-auth \
  --from-literal=api-key="$(openssl rand -hex 32)"
helm upgrade --install lummenna charts/lummenna -n lummenna \
  --set image.repository="$ACR/lummenna" --set image.tag=1.0.0 \
  --set auth.existingSecret=lummenna-auth \
  --set model.url="https://<blob>/models/bge-small-en-v1.5-r5c38ec7.tar.gz" \
  --set model.sha256="<tarball sha256>"
```

**Model lifecycle management:** package the ONNX model directory
(`model.onnx` + tokenizer files) as a tar.gz, upload to versioned/immutable
storage, pin `model.url` + `model.sha256` in values. Rolling a new model =
new artifact + new pin + `helm upgrade`; rollback = re-pin the old artifact.
The revision-pinned `lummenna model download` CLI (KNOWN_MODELS with commit
SHAs) covers non-Kubernetes hosts.

**Backups on AKS:** snapshot the `store` PVC (Azure Disk snapshots) on a
schedule; the nightly in-pod `backup_database` pass also writes into the
same volume.

---

## 5. Security Hardening

### 5.1 API Authentication

Lumena supports optional API key authentication via the `X-API-Key` header.

```bash
# 1. Generate a strong key
export LUMENA_API_KEY=$(openssl rand -hex 32)

# 2. Add it to your config
export LUMENA_API_KEY="$LUMENA_API_KEY"
```

**Authenticated request:**
```bash
curl -H "X-API-Key: $LUMENA_API_KEY" http://localhost:8848/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "sovereign AI memory", "top_k": 5}'
```

**Multi-tenant headers:**
```bash
curl -H "X-API-Key: $LUMENA_API_KEY" \
  -H "X-Tenant-ID: customer-acme" \
  http://localhost:8848/v1/status
```

> **Warning:** When `LUMENA_API_KEY` is unset, the API is open. Always set it before exposing Lumena to any network.

### 5.2 Encryption at Rest

Lumena does not yet implement built-in database encryption. Use OS-level full-disk encryption:

| OS | Technology | Enable |
|---|---|---|
| macOS | FileVault | System Preferences → Security → FileVault |
| Windows | BitLocker | Settings → Device encryption |
| Linux | LUKS/dm-crypt | `cryptsetup luksFormat /dev/sdX` |
| Raspberry Pi | LUKS + initramfs | See [Raspberry Pi LUKS guide](https://www.raspberrypi.com/documentation/computers/configuration.html) |

SQLCipher integration is planned for v0.3.0. On startup, Lumena logs a reminder:

```
encryption_at_rest_disabled
recommendation='Use OS-level disk encryption (FileVault, BitLocker, LUKS) for production'
```

### 5.3 File Permissions

File permissions are enforced automatically on startup (fail-fast if the process cannot chmod). The entire `~/.lumena` tree is set to `700` for directories and `600` for files. Ensure the Lumena process owns the path:

```bash
# Secure the Lumena home directory
chmod 700 ~/.lumena
chmod 600 ~/.lumena/config.toml
chmod 600 ~/.lumena/store/lumena.db
```

### 5.4 Systemd Service (Linux)

Create `/etc/systemd/system/lummenna.service`:

```ini
[Unit]
Description=Lumena Memory API Server
After=network.target

[Service]
Type=simple
User=lummenna
Group=lummenna
WorkingDirectory=/home/lummenna
Environment="LUMENA_DEVICE=generic"
Environment="LUMENA_LOG_LEVEL=info"
Environment="LUMENA_SOVEREIGN=true"
Environment="LUMENA_API_KEY=your-api-key-here"
Environment="PATH=/home/lummenna/.venvs/lummenna/bin"
ExecStart=/home/lummenna/.venvs/lummenna/bin/uvicorn lummenna.api.server:app --host 0.0.0.0 --port 8848
Restart=always
RestartSec=5
MemoryMax=900M
CPUQuota=80%
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/home/lummenna/.lumena

[Install]
WantedBy=multi-user.target
```

```bash
sudo useradd -r -s /bin/false lummenna
sudo mkdir -p /home/lummenna/.lumena
sudo chown -R lummenna:lummenna /home/lummenna/.lumena
sudo systemctl daemon-reload
sudo systemctl enable --now lummenna
```

### 5.5 Reverse Proxy (Nginx)

```nginx
server {
    listen 443 ssl http2;
    server_name lummenna.local;

    ssl_certificate /etc/ssl/certs/lummenna.crt;
    ssl_certificate_key /etc/ssl/private/lummenna.key;

    location / {
        proxy_pass http://127.0.0.1:8848;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Rate limiting
    limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
    limit_req zone=api burst=20 nodelay;
}
```

---

## 6. Backup & Disaster Recovery

### 6.1 What to Back Up

```bash
~/.lumena/config.toml          # Device profile and TFC settings
~/.lumena/palace.toml          # Room topology
~/.lumena/user.toml            # Per-user weights
~/.lumena/store/lumena.db       # SQLite database (WAL mode)
~/.lumena/store/lumena.db-wal   # WAL file (must be backed up together)
~/.lumena/store/vectors.usearch # USearch index (if used)
~/.lumena/logs/audit.jsonl     # Compliance audit trail
```

### 6.2 Automated Backup Script

```bash
#!/bin/bash
# /usr/local/bin/lummenna-backup.sh

BACKUP_DIR="/backup/lummenna/$(date +%Y%m%d_%H%M%S)"
mkdir -p "$BACKUP_DIR"

# Checkpoint WAL before backup
sqlite3 ~/.lumena/store/lumena.db "PRAGMA wal_checkpoint(TRUNCATE);"

# Atomic copy
cp ~/.lumena/config.toml "$BACKUP_DIR/"
cp ~/.lumena/palace.toml "$BACKUP_DIR/"
cp ~/.lumena/store/lumena.db "$BACKUP_DIR/"
cp -r ~/.lumena/logs "$BACKUP_DIR/"

# Compress
tar czf "${BACKUP_DIR}.tar.gz" -C "$(dirname "$BACKUP_DIR")" "$(basename "$BACKUP_DIR")"
rm -rf "$BACKUP_DIR"

# Retention: keep last 14 days
find /backup/lummenna -name "*.tar.gz" -mtime +14 -delete
```

---

## 7. Scaling & Performance

### 7.1 Footprints

Runtime RAM and storage footprints have **not yet been measured** — the perf suite (`python -m benchmarks.perf.run`) is a ready harness with no committed results. Until measured artifacts exist, size your deployment empirically: run the perf suite on your target hardware and check `process_peak_rss_mb` on the `/metrics` endpoint and `db_size_mb` on `/v1/dashboard-data`, which report real measured values for a running instance.

### 7.2 Performance Tuning

```bash
# Disable CPU frequency scaling for latency
sudo cpupower frequency-set -g performance

# Increase file descriptor limits
ulimit -n 65536

# Tune kernel for SQLite WAL
sudo sysctl -w vm.swappiness=1
sudo sysctl -w vm.dirty_ratio=5
sudo sysctl -w vm.dirty_background_ratio=2
```

### 7.3 Monitoring Metrics

Key metrics to alert on:
- `lumena_memory_usage_mb` > 80% of `LUMENA_MEMORY_LIMIT`
- `lumena_context_usage_ratio` > 0.95
- `lumena_forgetting_queue_depth` > 1000
- `lumena_retrieval_latency_p99` > 500ms
- `lumena_consolidation_failures` > 0 in 1h

---

## 8. Upgrade Procedures

### 8.1 In-Place Upgrade

```bash
# 1. Backup
lummenna-backup.sh

# 2. Stop service
sudo systemctl stop lummenna

# 3. Upgrade
source ~/.venvs/lummenna/bin/activate
pip install --upgrade lummenna

# 4. Migrations run automatically on DB connect; nothing to do manually
# 5. Restart
sudo systemctl start lummenna

# 6. Verify
lummenna status
curl http://localhost:8848/health
```

### 8.2 Rollback

```bash
# Restore from backup
sudo systemctl stop lummenna
sqlite3 ~/.lumena/store/lumena.db ".restore '${BACKUP_DIR}/lumena.db'"
# Reinstall previous version
pip install lummenna==$PREVIOUS_VERSION
sudo systemctl start lummenna
```

---

## 9. Troubleshooting

### 9.1 Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `sqlite3.OperationalError: database is locked` | WAL checkpoint stuck | `PRAGMA wal_checkpoint(TRUNCATE);` |
| High RAM usage | USearch index loaded into RAM | Switch to `vector_index = "sqlite-vec"` |
| Slow retrieval | No FTS5 index | Ensure schema is initialized with `lummenna init` |
| `SovereignViolation` error | External API blocked | Set `LUMENA_SOVEREIGN=false` or use offline embedder |
| SD card wear | Too many random writes | Enable WAL + `synchronous=NORMAL` |

### 9.2 Diagnostic Commands

```bash
# Full system report
lummenna status

# Check database integrity
sqlite3 ~/.lumena/store/lumena.db "PRAGMA integrity_check;"

# View recent audit events (last N entries)
lummenna compliance audit --n 10

# Test retrieval latency via the API
curl -X POST http://localhost:8848/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "test query", "top_k": 5}'
```

---

## 10. Support & Community

- **Documentation:** https://github.com/QuantmindSSI/lummenna/tree/main/docs
- **Issues:** https://github.com/QuantmindSSI/lummenna/issues
- **Discussions:** https://github.com/QuantmindSSI/lummenna/discussions
- **Security:** security@lummenna.ai
- **Matrix:** #lummenna:matrix.org

---

*Last updated: 2026-07-20*  
*Maintainer: Lumena Engineering Team*
