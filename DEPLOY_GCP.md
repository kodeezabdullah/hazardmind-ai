# Deploying HazardMind AI on Google Cloud Platform

This is a step-by-step, copy-paste guide for a **first-time GCP deployment**.
HazardMind runs as **five independent services**:

| Service | Type | Entry point | Recommended size |
|---|---|---|---|
| `backend` | HTTP API | `uvicorn main:app` | 2 GB RAM (Cloud Run or small VM) |
| `satellite` | Band listener | `agent.py` | **16 GB RAM** (heavy raster I/O) |
| `hazard` | Band listener | `agent.py` | 4 GB RAM |
| `impact` | Band listener | `agent.py` | 4 GB RAM |
| `report` | Band listener | `band_agent.py` | 4 GB RAM |

The four agents are **background listeners** (they connect to the Band network,
they do not serve HTTP). The cleanest way to run a background listener on GCP is a
**Compute Engine VM running the Docker container**. The backend API can run on a
small VM the same way, or on Cloud Run.

Every service has a `Dockerfile` already. GDAL/GEOS/PROJ system libraries (needed
by the satellite/hazard/impact agents) are installed inside the image, so you do
not install anything on the VM except Docker.

---

## 0. One-time setup

Install the gcloud CLI and log in:

```bash
# https://cloud.google.com/sdk/docs/install
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

Enable the APIs you need:

```bash
gcloud services enable compute.googleapis.com artifactregistry.googleapis.com
```

Create an Artifact Registry repo to hold the images:

```bash
gcloud artifacts repositories create hazardmind \
  --repository-format=docker \
  --location=us-central1 \
  --description="HazardMind images"

gcloud auth configure-docker us-central1-docker.pkg.dev
```

Set a couple of shell variables (adjust the region/project):

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export REGION=us-central1
export REPO=us-central1-docker.pkg.dev/$PROJECT_ID/hazardmind
```

---

## 1. Build and push every image

Run these from the repository root. Each builds the service image and pushes it
to Artifact Registry.

```bash
# Satellite (largest image, has GDAL)
docker build -t $REPO/satellite:latest agents/satellite
docker push  $REPO/satellite:latest

# Hazard
docker build -t $REPO/hazard:latest agents/hazard
docker push  $REPO/hazard:latest

# Impact
docker build -t $REPO/impact:latest agents/impact
docker push  $REPO/impact:latest

# Report
docker build -t $REPO/report:latest agents/report
docker push  $REPO/report:latest

# Backend API
docker build -t $REPO/backend:latest backend
docker push  $REPO/backend:latest
```

> If you do not have Docker locally, you can build in the cloud instead:
> `gcloud builds submit agents/satellite --tag $REPO/satellite:latest` (repeat per service).

---

## 2. Prepare each service's environment file

Each service reads its own `.env`. These are NOT in the image (they are
git-ignored and docker-ignored). You will pass them to the container at runtime.

Make sure each of these files is filled in locally:

```
backend/.env
agents/satellite/.env
agents/hazard/.env
agents/impact/.env
agents/report/.env
```

For VM deployment we will copy the relevant `.env` to the VM and pass it with
`--env-file`. (For a stronger setup, use Google Secret Manager — see the note at
the end.)

---

## 3. Deploy the agents on Compute Engine VMs

The pattern below creates a VM, installs Docker, and runs the container with the
service's `.env`. The VM uses a **Container-Optimized OS**, so Docker is already
present.

### 3a. Satellite agent (16 GB RAM)

```bash
gcloud compute instances create-with-container hazardmind-satellite \
  --zone=${REGION}-a \
  --machine-type=e2-standard-4 \
  --boot-disk-size=50GB \
  --container-image=$REPO/satellite:latest \
  --container-restart-policy=always
```

`e2-standard-4` is 4 vCPU / 16 GB RAM — the satellite agent's raster mosaic step
benefits from this. The 50 GB disk gives room for the temporary band downloads.

Now copy the env file onto the VM and restart the container with it. The simplest
reliable way is to pass env vars at creation time. Re-create with the env file:

```bash
# Delete the placeholder and recreate with the env file mounted as container env.
gcloud compute instances delete hazardmind-satellite --zone=${REGION}-a --quiet

# Build a --container-env flag list from the .env (skips comments/blank lines):
ENVFLAGS=$(grep -vE '^\s*#|^\s*$' agents/satellite/.env | sed 's/^/--container-env=/' | tr '\n' ' ')

gcloud compute instances create-with-container hazardmind-satellite \
  --zone=${REGION}-a \
  --machine-type=e2-standard-4 \
  --boot-disk-size=50GB \
  --container-image=$REPO/satellite:latest \
  --container-restart-policy=always \
  $ENVFLAGS
```

### 3b. Hazard / Impact / Report agents (4 GB each)

Same pattern, smaller machine (`e2-medium` = 1 vCPU / 4 GB):

```bash
for SVC in hazard impact report; do
  ENVFLAGS=$(grep -vE '^\s*#|^\s*$' agents/$SVC/.env | sed 's/^/--container-env=/' | tr '\n' ' ')
  gcloud compute instances create-with-container hazardmind-$SVC \
    --zone=${REGION}-a \
    --machine-type=e2-medium \
    --boot-disk-size=20GB \
    --container-image=$REPO/$SVC:latest \
    --container-restart-policy=always \
    $ENVFLAGS
done
```

---

## 4. Deploy the backend API

The backend serves HTTP, so it needs an open port. Two options:

### Option A — VM (consistent with the agents)

```bash
ENVFLAGS=$(grep -vE '^\s*#|^\s*$' backend/.env | sed 's/^/--container-env=/' | tr '\n' ' ')

gcloud compute instances create-with-container hazardmind-backend \
  --zone=${REGION}-a \
  --machine-type=e2-small \
  --boot-disk-size=20GB \
  --container-image=$REPO/backend:latest \
  --container-restart-policy=always \
  --tags=http-server \
  $ENVFLAGS

# Allow inbound HTTP on 8000
gcloud compute firewall-rules create allow-hazardmind-api \
  --allow=tcp:8000 \
  --target-tags=http-server \
  --description="HazardMind backend API"
```

The API will be reachable at `http://<VM_EXTERNAL_IP>:8000`. For HTTPS, put it
behind a load balancer or run a reverse proxy (Caddy/Nginx) on the VM.

### Option B — Cloud Run (managed, auto-HTTPS)

```bash
gcloud run deploy hazardmind-backend \
  --image=$REPO/backend:latest \
  --region=$REGION \
  --allow-unauthenticated \
  --memory=2Gi \
  --set-env-vars="$(grep -vE '^\s*#|^\s*$' backend/.env | tr '\n' ',' | sed 's/,$//')"
```

Cloud Run gives you an HTTPS URL automatically. Set `ALLOWED_ORIGINS` in the env
to your frontend domain (CORS is env-driven).

---

## 5. Verify

```bash
# Backend health
curl http://<BACKEND_HOST>/health

# Trigger a run
curl -X POST http://<BACKEND_HOST>/analyze \
  -H "Content-Type: application/json" \
  -d '{"location":"Rawalpindi","disaster_type":"flood","magnitude":0}'

# Poll
curl http://<BACKEND_HOST>/status/<job_id>
curl http://<BACKEND_HOST>/results/<job_id>
```

Watch agent logs:

```bash
gcloud compute ssh hazardmind-satellite --zone=${REGION}-a --command="docker logs \$(docker ps -q)"
```

---

## 6. Updating a service

After a code change, rebuild + push the image and recreate that one VM (or, on
Cloud Run, just `gcloud run deploy` again):

```bash
docker build -t $REPO/hazard:latest agents/hazard && docker push $REPO/hazard:latest
gcloud compute instances delete hazardmind-hazard --zone=${REGION}-a --quiet
# ...then recreate it with the create-with-container command from step 3b.
```

---

## Notes & best practices

- **Secrets**: passing `.env` via `--container-env` is fine to start. For
  production, store keys in **Google Secret Manager** and reference them, so
  secrets never sit on the VM.
- **Cost**: the agents run continuously (they listen on Band), so the VMs are
  always-on. `e2-medium` agents are cheap; the satellite `e2-standard-4` is the
  main cost. Stop VMs when not demoing to save credits:
  `gcloud compute instances stop hazardmind-satellite --zone=${REGION}-a`.
- **Region**: keep all services + the Neon DB region close to reduce latency.
- **Frontend**: deploy the Next.js frontend separately (e.g. Vercel) and set
  `NEXT_PUBLIC_API_URL` to the backend's public URL.
- **GDAL**: already handled inside the satellite/hazard/impact images — nothing
  to install on the VM.
