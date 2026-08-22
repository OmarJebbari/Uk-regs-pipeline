# Backup & Disaster Recovery Runbook

This runbook covers the **three stateful components** of the UK Regs Pipeline:
PostgreSQL (rules, manifests, change-sets, vectors), Neo4j (supersession /
dependency graph), and MinIO/S3 (immutable PDF originals + figures). The API
and MCP server are stateless — redeploy them from images; nothing to restore.

> **RPO/RTO targets (dev/prod profile below):** RPO ≤ 24 h for full snapshots,
> ≤ 5 min for Postgres WAL archiving (prod). RTO ≤ 2 h for full regional restore.

---

## 1. What needs backing up

| Component | Holds | Dev (compose) | Prod (Terraform) |
|---|---|---|---|
| **PostgreSQL 16 + pgvector** | rules, citations, change-sets, embeddings, source state | `pgdata` volume | RDS Multi-AZ + PITR |
| **Neo4j 5** | `SUPERSEDED_BY`, `REFERENCES`, dependency edges | `neo4j` volume | self-managed / restore from dump |
| **MinIO / S3** | immutable PDFs (`documents/…`), extracted figures | `minio-data` volume | S3 versioned buckets |
| **`.env` / secrets** | DB creds, API keys | local file | Secrets Manager |

**Restore-order dependency:** Postgres first (source of truth), then Neo4j
(edges reference rule_ids), then MinIO (blobs keyed by sha — safe to restore
in parallel, but ingestion needs it).

---

## 2. Backup procedures

### 2.1 Postgres — logical dump (dev + prod weekly)

```bash
# Dev (docker compose data plane):
docker compose exec -T postgres pg_dump -U ukregs --format=custom \
  --file=/backups/ukregs-$(date +%F).dump ukregs
# copy out of the container / volume:
docker compose cp postgres:/backups/ukregs-$(date +%F).dump ./backups/

# Prod (RDS): use any bastion/EC2 with psql, or AWS Data Lifecycle Manager
# for automated snapshots. PITR is already on — see §2.4.
```

Automate daily via cron:

```cron
0 2 * * *  cd /srv/uk-regs-pipeline && make pg-backup >> backups/log 2>&1
```

### 2.2 Neo4j — `dump` (dev) / `neo4j-admin` (prod)

```bash
docker compose stop neo4j
docker compose run --rm -v ./backups:/data neo4j \
  neo4j-admin database dump ukregs --to-stdout > backups/neo4j-$(date +%F).dump
docker compose start neo4j
```

### 2.3 MinIO — mirror buckets

```bash
# Dev: use `mc` (MinIO client) or simply back up the volume directory:
mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc mirror --overwrite local/uk-regs ./backups/minio/

# Prod: S3 versioning + cross-region replication are already enabled in
# Terraform (infra/terraform); verify with:
aws s3api get-bucket-versioning --bucket <originals-bucket>
```

### 2.4 Prod continuous protection (already in Terraform)

- **RDS:** automated backups (daily) + point-in-time recovery window (default 7 days),
  Multi-AZ sync standby.
- **S3:** bucket versioning ON — accidental deletes are recoverable via versions.
- **Secrets Manager:** secrets are versioned automatically.

Verify settings after `make tf-apply`:

```bash
aws rds describe-db-instances --db-instance-identifier ukregs \
  --query 'DBInstances[0].[BackupRetentionPeriod,MultiAZ]'
```

---

## 3. Restore procedures (drills)

> 📅 **Run a restore drill quarterly.** An untested backup is not a backup.
> Record the drill in `docs/BACKUP-DR.md` "Drill log" at the bottom.

### 3.1 Postgres

```bash
docker compose exec -T postgres pg_restore -U ukregs --clean --if-exists \
  --dbname=ukregs /backups/ukregs-<DATE>.dump
```

Point-in-time (prod RDS):

```bash
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier ukregs \
  --target-db-instance-identifier ukregs-restore \
  --restore-time 2026-08-22T10:00:00Z
```

### 3.2 Neo4j

```bash
docker compose stop neo4j
docker compose run --rm -v ./backups:/backups neo4j \
  sh -c 'neo4j-admin database load ukregs --from-path=/backups/neo4j-<DATE>.dump --overwrite'
docker compose start neo4j
```

### 3.3 MinIO / S3

```bash
mc mirror --overwrite ./backups/minio/ local/uk-regs
```

### 3.4 Full-stack sanity check after restore

```bash
make init-db            # schema is idempotent, fills gaps
make eval               # golden gate must stay 100%
curl -s localhost:8000/healthz && curl -s localhost:8000/ops/stats | jq .
```

If `/ops/stats` shows rules > 0 and evals pass, the restore is good.

---

## 4. Disaster scenarios

| Scenario | Immediate action | Recovery |
|---|---|---|
| **Postgres data corruption** | stop writers (`docker compose stop api`), snapshot volume | `pg_restore` latest dump or RDS PITR |
| **Accidental rule deletion** | — | RDS PITR / dump restore into temp DB, re-upsert lost rows |
| **Volume/host loss (dev)** | — | re-provision host, `make up`, restore §3 |
| **Region loss (prod)** | declare incident | redeploy Terraform in DR region, restore RDS cross-region snapshot, S3 CRR copy |
| **Secrets leak** | rotate in Secrets Manager, redeploy API | no data restore needed |
| **Bad ingest poisoned corpus** | stop source-watch | `--force` re-ingest the *good* PDF; supersession restores prior truth; or restore DB from pre-ingest snapshot |

---

## 5. Retention & monitoring

- Dumps: keep **30 days** locally, **1 year** in cold storage (S3 Glacier).
- Prod RDS PITR window: 7 days (raise to 35 if compliance demands).
- Alert on backup job failure — wire the cron exit code to your alerting, or
  enable AWS Backup plans via Terraform.

---

## Drill log

| Date | Scope | Duration | Operator | Result |
|---|---|---|---|---|
| — | — | — | — | _no drills yet_ |