#!/usr/bin/env bash
# Deploy the sourcing agent POC to Cloud Run.
#
# No authentication and no Secret Manager - both are out of scope for this POC.
# The backend is public and the database password is a plain environment
# variable, so do not put real supplier data in an environment deployed this
# way.
#
# Usage:  PROJECT_ID=my-project ./infra/deploy.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
REGION="${REGION:-europe-west3}"
DOCAI_LOCATION="${DOCAI_LOCATION:-eu}"
DOCAI_PROCESSOR_ID="${DOCAI_PROCESSOR_ID:?set DOCAI_PROCESSOR_ID}"
VERTEX_MODEL="${VERTEX_MODEL:-gemini-2.5-flash}"
# Model availability is region specific; the global endpoint works everywhere.
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"

SQL_INSTANCE="${SQL_INSTANCE:-quote-poc-db}"
DB_NAME="${DB_NAME:-quotepoc}"
DB_USER="${DB_USER:-postgres}"
DB_PASSWORD="${DB_PASSWORD:?set DB_PASSWORD}"

BUCKET="${BUCKET:-${PROJECT_ID}-quote-poc}"
REPO="${REPO:-quote-poc}"
BACKEND_SERVICE="${BACKEND_SERVICE:-quote-backend}"
FRONTEND_SERVICE="${FRONTEND_SERVICE:-quote-frontend}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-quote-poc-run}"

IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"
SA_EMAIL="${SERVICE_ACCOUNT}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "${PROJECT_ID}" >/dev/null

say() { printf "\n\033[1m==> %s\033[0m\n" "$1"; }

say "Enabling APIs"
gcloud services enable \
  run.googleapis.com \
  sqladmin.googleapis.com \
  documentai.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com

say "Artifact Registry"
gcloud artifacts repositories describe "${REPO}" --location "${REGION}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${REPO}" \
  --repository-format=docker --location="${REGION}" \
  --description="Sourcing agent POC images"

say "Cloud Storage bucket"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 || \
gcloud storage buckets create "gs://${BUCKET}" --location="${REGION}" --uniform-bucket-level-access

say "Cloud SQL for PostgreSQL"
# db-f1-micro is the cheapest tier and is plenty for three quotes.
gcloud sql instances describe "${SQL_INSTANCE}" >/dev/null 2>&1 || \
gcloud sql instances create "${SQL_INSTANCE}" \
  --database-version=POSTGRES_15 --tier=db-f1-micro --region="${REGION}" \
  --storage-size=10GB --no-backup

gcloud sql users set-password "${DB_USER}" --instance="${SQL_INSTANCE}" \
  --password="${DB_PASSWORD}" >/dev/null

gcloud sql databases describe "${DB_NAME}" --instance="${SQL_INSTANCE}" >/dev/null 2>&1 || \
gcloud sql databases create "${DB_NAME}" --instance="${SQL_INSTANCE}"

INSTANCE_CONNECTION_NAME="$(gcloud sql instances describe "${SQL_INSTANCE}" \
  --format='value(connectionName)')"

say "Service account and roles"
gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1 || \
gcloud iam service-accounts create "${SERVICE_ACCOUNT}" \
  --display-name="Sourcing agent POC runtime"

for role in \
  roles/cloudsql.client \
  roles/storage.objectAdmin \
  roles/documentai.apiUser \
  roles/aiplatform.user
do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" --role="${role}" \
    --condition=None >/dev/null
done

say "Building backend image"
gcloud builds submit backend --tag "${IMAGE_BASE}/backend:latest"

say "Deploying backend"
# --no-cpu-throttling is required, not a tuning choice: extraction runs in a
# FastAPI background task after the response has been sent, and Cloud Run's
# default throttles CPU to near zero at that point, which would stall every
# upload at PROCESSING.
#
# The ^@^ prefix makes @ the env-var separator so a password containing a comma
# does not split the list.
#
# The schema is created on start-up, so there is no migration step.
gcloud run deploy "${BACKEND_SERVICE}" \
  --image "${IMAGE_BASE}/backend:latest" \
  --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --add-cloudsql-instances "${INSTANCE_CONNECTION_NAME}" \
  --memory 1Gi --cpu 1 --timeout 600 --max-instances 3 \
  --no-cpu-throttling \
  --set-env-vars "^@^GCP_PROJECT=${PROJECT_ID}@GCS_BUCKET=${BUCKET}@DOCAI_LOCATION=${DOCAI_LOCATION}@DOCAI_PROCESSOR_ID=${DOCAI_PROCESSOR_ID}@VERTEX_LOCATION=${VERTEX_LOCATION}@VERTEX_MODEL=${VERTEX_MODEL}@INSTANCE_CONNECTION_NAME=${INSTANCE_CONNECTION_NAME}@DB_USER=${DB_USER}@DB_PASSWORD=${DB_PASSWORD}@DB_NAME=${DB_NAME}"

BACKEND_URL="$(gcloud run services describe "${BACKEND_SERVICE}" \
  --region "${REGION}" --format='value(status.url)')"

say "Building frontend image"
gcloud builds submit frontend --tag "${IMAGE_BASE}/frontend:latest"

say "Deploying frontend"
# nginx proxies /api to the backend, so the browser only ever talks to one origin.
gcloud run deploy "${FRONTEND_SERVICE}" \
  --image "${IMAGE_BASE}/frontend:latest" \
  --region "${REGION}" \
  --allow-unauthenticated \
  --memory 256Mi --max-instances 3 \
  --set-env-vars "BACKEND_URL=${BACKEND_URL}"

FRONTEND_URL="$(gcloud run services describe "${FRONTEND_SERVICE}" \
  --region "${REGION}" --format='value(status.url)')"

say "Done"
echo "Frontend: ${FRONTEND_URL}"
echo "Backend:  ${BACKEND_URL}"
echo "Database: ${INSTANCE_CONNECTION_NAME}"
