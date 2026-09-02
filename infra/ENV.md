# Manual Cloud Run setup

Deploy the **backend first** — the frontend needs its URL.

## Enable these APIs

```bash
gcloud services enable run.googleapis.com sqladmin.googleapis.com documentai.googleapis.com aiplatform.googleapis.com storage.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com
```

## Service account

One account for the backend. The frontend is nginx and needs no permissions.

```bash
gcloud iam service-accounts create quote-poc-run --display-name="Sourcing agent POC runtime"
```

Grant it four roles:

| Role | For |
|---|---|
| `roles/cloudsql.client` | Cloud SQL over the unix socket |
| `roles/storage.objectAdmin` | Source PDFs and raw Document AI responses |
| `roles/documentai.apiUser` | Calling the quotation processor |
| `roles/aiplatform.user` | Gemini on Vertex AI |

```bash
for role in roles/cloudsql.client roles/storage.objectAdmin roles/documentai.apiUser roles/aiplatform.user; do gcloud projects add-iam-policy-binding PROJECT_ID --member="serviceAccount:quote-poc-run@PROJECT_ID.iam.gserviceaccount.com" --role="$role" --condition=None; done
```

## Backend environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `GCP_PROJECT` | **yes** | — | Project id. Used for the Vertex, Storage and Document AI clients |
| `GCS_BUCKET` | **yes** | — | Bucket **name only**, no `gs://` prefix |
| `DOCAI_LOCATION` | **yes** | `eu` | Must match the processor's region — `eu` or `us`. A mismatch gives a confusing 404 |
| `DOCAI_PROCESSOR_ID` | **yes** | — | The processor **id only**, not the full `projects/.../processors/...` path |
| `INSTANCE_CONNECTION_NAME` | **yes** | — | `project:region:instance` from the Cloud SQL overview page |
| `DB_PASSWORD` | **yes** | — | Password for `DB_USER` |
| `DB_USER` | no | `postgres` | |
| `DB_NAME` | no | `quotepoc` | The database must already exist |
| `VERTEX_LOCATION` | no | `global` | Gemini availability is region specific; `global` works everywhere |
| `VERTEX_MODEL` | no | `gemini-2.5-flash` | |
| `AGENT_TIMEOUT_SECONDS` | no | `120` | Budget for one agent call. Past it the deterministic answer is used. Keep it under the nginx (300s) and Cloud Run request timeouts |
| `DATABASE_URL` | no | — | Full SQLAlchemy URL. If set it overrides every `DB_*` and `INSTANCE_CONNECTION_NAME` value above |
| `PLANT_NAME` | no | — | Site named on the approval package. Unset, the destination clause is omitted |
| `PORT` | no | `8080` | Cloud Run sets this itself — do not override |
| `GOOGLE_GENAI_USE_VERTEXAI` | no | derived `TRUE` | See below — set only to override |
| `GOOGLE_CLOUD_PROJECT` | no | derived from `GCP_PROJECT` | |
| `GOOGLE_CLOUD_LOCATION` | no | derived from `VERTEX_LOCATION` | |

### Why the three `GOOGLE_*` variables exist

ADK does not take this application's settings. It builds its own
`google.genai` client and reads those three names. With none of them set the
client defaults to the **Gemini Developer API** and raises `No API key was
provided` before a single request leaves the process.

That failure is close to invisible. The agent's caller falls back to the
deterministic memo, so a buyer still gets a correct answer — and the only
outward sign is that no `call_llm` or `execute_tool` span ever appears in
Trace Explorer.

`config.configure_agent_env()` derives them at start-up with `setdefault`, so
setting any of them explicitly still wins. Set them only to point the agent at
a different project or region than Document AI and Storage use.

## Telemetry

Traces go to Cloud Trace, logs go to stdout as structured JSON carrying the
trace id, and Cloud Run picks those up on its own. Nothing here is required:
with none of it set the app runs and exports traces as long as `GCP_PROJECT` is
set and the service account can write them.

Telemetry never blocks start-up. If the exporter cannot be built — no
credentials, missing role, API not enabled — the app logs the reason, serves
normally, and `GET /healthz` reports it:

```json
{"status": "ok", "telemetry": "trace export unavailable: DefaultCredentialsError: ..."}
```

| Variable | Default | Notes |
|---|---|---|
| `OTEL_DISABLED` | `0` | **Master switch.** `1` installs no exporter, no provider and no instrumentation — nothing is wrapped, so there is no span-building overhead either. stdout logging is unaffected. `/healthz` reports it |
| `TRACE_SAMPLING_RATIO` | `1.0` | Fraction of traces kept. Lower it if the agent's spans get expensive |
| `OTEL_LOGS_EXPORT` | `0` | `1` also ships log records through the Cloud Logging API. Leave off on Cloud Run — stdout is already collected, so enabling this writes every line twice |
| `LOG_LEVEL` | `INFO` | Root logger level |
| `OTEL_SERVICE_NAME` | `K_SERVICE` | Service name in Trace Explorer |
| `ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS` | `true` | Prompt capture on ADK's **legacy** path. Ignored once `OTEL_SEMCONV_STABILITY_OPT_IN` is set |
| `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT` | — | Prompt capture on the **experimental** path. `SPAN_ONLY`, `EVENT_ONLY` or `SPAN_AND_EVENT` only — any other value, `true` included, captures nothing |
| `OTEL_SEMCONV_STABILITY_OPT_IN` | — | `gen_ai_latest_experimental` switches ADK to the newer GenAI semantic conventions |

Two IAM roles are needed on the runtime service account, and neither is granted
by `deploy.sh`:

```
roles/cloudtrace.agent
roles/logging.logWriter
```

### What a trace looks like

An agent question produces one trace. `call_llm` and `execute_tool` come from
ADK's own instrumentation, which finds the tracer provider this app installs at
start-up — ADK is not configured for it anywhere:

```
POST /api/runs/{run_id}/ask
└── agent.explain
    └── invocation
        └── invoke_agent sourcing_agent
            ├── call_llm
            │   └── generate_content gemini-2.5-flash    gen_ai.* attributes
            │       └── execute_tool get_run_summary
            │           └── SELECT evaluation_run
            └── call_llm
                └── generate_content gemini-2.5-flash
```

Extraction runs in a background task after the response is sent, but Starlette
still runs it inside the request's context, so it nests under the upload rather
than starting a trace of its own — one trace covers the whole upload:

```
POST /api/comparisons/{id}/documents
└── ingest.process_document
    ├── docai.extract
    ├── gemini.normalize
    │   └── vertex.generate_json     (called twice: fields, then compliance)
    └── validate.quote
```

## Backend Cloud Run settings

Two of these are not defaults and the POC does not work without them.

| Setting | Value | Why |
|---|---|---|
| **CPU allocation** | **CPU is always allocated** | Not optional. Extraction runs in a background task *after* the response is sent, and the default throttles CPU to near zero at exactly that moment — every upload would stick at `PROCESSING` forever. Console: *Container → Resources → CPU allocation*. CLI: `--no-cpu-throttling` |
| **Cloud SQL connection** | Add your instance | Console: *Container → Connections → Cloud SQL connections*. CLI: `--add-cloudsql-instances PROJECT:REGION:INSTANCE` |
| Service account | `quote-poc-run@…` | |
| Authentication | Allow unauthenticated | No auth in this POC |
| Memory | 1 GiB | |
| Request timeout | 600s | |
| Max instances | 3 | |

```bash
gcloud run deploy quote-backend \
  --image REGION-docker.pkg.dev/PROJECT_ID/quote-poc/backend:latest \
  --region REGION \
  --service-account quote-poc-run@PROJECT_ID.iam.gserviceaccount.com \
  --allow-unauthenticated \
  --add-cloudsql-instances PROJECT:REGION:INSTANCE \
  --no-cpu-throttling \
  --memory 1Gi --cpu 1 --timeout 600 --max-instances 3 \
  --set-env-vars "^@^GCP_PROJECT=PROJECT_ID@GCS_BUCKET=BUCKET@DOCAI_LOCATION=eu@DOCAI_PROCESSOR_ID=PROCESSOR_ID@VERTEX_LOCATION=global@VERTEX_MODEL=gemini-2.5-flash@INSTANCE_CONNECTION_NAME=PROJECT:REGION:INSTANCE@DB_USER=postgres@DB_PASSWORD=PASSWORD@DB_NAME=quotepoc"
```

The `^@^` prefix makes `@` the separator instead of a comma, so a password
containing a comma does not silently split into two variables. If you set the
variables in the console instead, this does not apply.

## Frontend environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `BACKEND_URL` | **yes** | — | The backend's Cloud Run URL, e.g. `https://quote-backend-abc123-ew.a.run.app`. **No trailing slash and no `/api`** — nginx appends the path |
| `PORT` | no | `8080` | Cloud Run sets this itself |

nginx proxies `/api` to the backend, so the browser only ever talks to one
origin. Nothing else needs configuring.

```bash
gcloud run deploy quote-frontend \
  --image REGION-docker.pkg.dev/PROJECT_ID/quote-poc/frontend:latest \
  --region REGION \
  --allow-unauthenticated \
  --memory 256Mi --max-instances 3 \
  --set-env-vars "BACKEND_URL=https://quote-backend-xxxx.a.run.app"
```

## Cloud SQL

PostgreSQL 15. `db-f1-micro` is enough for this POC. The database must exist
before the backend starts; the **tables do not** — they are created on start-up,
along with the reference data.

```bash
gcloud sql instances create quote-poc-db --database-version=POSTGRES_15 --tier=db-f1-micro --region=REGION --storage-size=10GB --no-backup
gcloud sql databases create quotepoc --instance=quote-poc-db
gcloud sql users set-password postgres --instance=quote-poc-db --password=PASSWORD
```

No public IP is needed — the connection goes over the Cloud Run unix socket.

## Checking it came up

```bash
curl https://quote-backend-xxxx.a.run.app/healthz
```

Expect `{"status":"ok","engine_version":"1.0.0"}`.

Then check the reference data loaded:

```bash
curl https://quote-backend-xxxx.a.run.app/api/reference
```

Expect 5 materials, 5 benchmarks, 5 demand lines, 3 freight rows, 7 compliance
requirements and 11 policy values. The startup log line reads
`schema ready, 18 tables present` followed by `seeded 36 reference rows`.

## If something fails

| Symptom | Cause |
|---|---|
| Documents stay at `PROCESSING` | CPU allocation is not set to always allocated |
| `DOCAI_PROCESSOR_ID is not set` | Variable missing or holds the full resource path instead of the id |
| Document AI 404 | `DOCAI_LOCATION` does not match the processor's region |
| `GCS_BUCKET is not set` | Variable missing, or set with a `gs://` prefix |
| Startup fails on the database | `INSTANCE_CONNECTION_NAME` wrong, the Cloud SQL connection was not added to the service, or the database does not exist |
| Frontend loads but every call 502s | `BACKEND_URL` has a trailing slash, or includes `/api` |
| Status `FAILED` on a document | Read `error_detail` in the document list — the exception is stored there verbatim |
