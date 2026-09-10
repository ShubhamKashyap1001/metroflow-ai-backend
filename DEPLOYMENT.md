# MetroFlow — Deployment Guide (Milestone 4)

This covers the Milestone 4 deliverables: containerizing the backend
with Docker, wiring up CI/CD, and deploying to a cloud platform. Two
deployment targets are supported side by side — **Render** (simplest,
free, no credit card) and **AWS EC2** (matches the original spec's
"AWS or Azure" wording, needs a credit card even on the free tier).
You can use either one, or both — the same Dockerfile and the same
GitHub Actions workflow serve both without any changes.

## 1. What's in this repo for deployment

| File | Purpose |
|---|---|
| `Dockerfile` | Multi-stage build for the FastAPI backend. Used by both Render and the AWS path. |
| `.dockerignore` | Keeps the image lean (excludes tests, docs, git history). |
| `docker-compose.yml` | Local dev: builds the image from source + Postgres + Redis, one command. |
| `docker-compose.prod.yml` | Used on the EC2 box: pulls a pre-built image from Docker Hub instead of building on the server. |
| `.env.example` | Every environment variable the app reads, with comments. |
| `.github/workflows/deploy.yml` | CI/CD: runs tests, then deploys to whichever platform(s) have secrets configured. |

## 2. Local development (test the container yourself first)

```bash
cp .env.example .env   # fill in your Supabase keys
docker compose up -d --build
curl http://localhost:8000/healthz   # should return {"status":"ok"}
```

## 3. Option A — Render (recommended: free, no credit card)

1. Push this repo to GitHub.
2. On [render.com](https://render.com), sign up with GitHub (no card
   asked).
3. **New > PostgreSQL** — Free plan. Copy the Internal Database URL.
4. **New > Key Value** — Free plan. Copy the Internal Redis URL.
5. **New > Web Service** — connect this repo, Environment: **Docker**,
   Instance Type: **Free**.
6. Environment tab — add: `DATABASE_URL`, `REDIS_URL`, `SUPABASE_URL`,
   `SUPABASE_KEY`, `SUPABASE_JWT_SECRET`, `CORS_ORIGINS`,
   `ENABLE_SIMULATOR=True`, `ENABLE_TRAIN_TRACKING=True`,
   `WEB_CONCURRENCY=1`.
7. Settings — Pre-Deploy Command: `alembic upgrade head`.
8. Create Web Service. First build takes 5–10 minutes.
9. (Optional, for CI/CD-gated deploys) Settings > turn off
   "Auto-Deploy", then Settings > Deploy Hook > copy the URL into
   the GitHub secret `RENDER_DEPLOY_HOOK_URL`. Now Render only
   deploys after GitHub Actions' tests pass, instead of on every push
   regardless of test result.

**Render free-tier facts:** the free Postgres expires 30 days after
creation (re-seed after recreating it); the free web service sleeps
after 15 minutes idle and takes ~50s to wake on the next request.
Fine for a demo/project; not for production traffic.

## 4. Option B — AWS EC2 (matches "AWS" in the spec, needs a card)

1. Create an AWS account (credit card required even for free tier).
2. **Immediately** set a Billing Budget alert at $1 so you're warned
   before anything is ever charged.
3. Launch a `t2.micro`/`t3.micro` (free-tier-eligible) Ubuntu 22.04
   EC2 instance. Security group: allow SSH (22) and HTTP (80).
4. SSH in, install Docker: `sudo apt update && sudo apt install -y
   docker.io docker-compose-v2 git`, then `sudo usermod -aG docker
   $USER` and reconnect.
5. `git clone <your-repo-url> metroflow && cd metroflow`
6. `cp .env.example .env` and fill in real values (Supabase keys,
   `CORS_ORIGINS`, etc.) — also add `DOCKERHUB_IMAGE=` (CI/CD fills
   this in automatically on every deploy; leave it blank for now).
7. First manual run: `docker compose up -d --build` and confirm
   `curl http://localhost:8000/healthz` works.
8. For CI/CD to take over from here, add these GitHub secrets:
   `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` (Docker Hub > Account
   Settings > Security > New Access Token), `EC2_HOST` (the
   instance's public IP), `EC2_USER` (`ubuntu`), `EC2_SSH_KEY` (the
   full contents of the `.pem` key file).

**Why Docker Hub instead of AWS ECR:** ECR's free tier caps out at
500MB of image storage, and this image (numpy/pandas/scikit-learn/
xgboost baked in) can exceed that — risking a storage charge. Docker
Hub's free tier has no such cap.

**Staying free on AWS:** one EC2 instance only, no RDS, no
ElastiCache, no unattached Elastic IPs, Basic (free) support plan.
Free tier expires 12 months after account creation — after that the
same instance becomes a paid ~$8–9/month box.

## 5. CI/CD behavior (`.github/workflows/deploy.yml`)

On every push to `main`:

1. `test` job always runs (`pytest`). Everything else waits on this.
2. `deploy-render` runs **only if** `RENDER_DEPLOY_HOOK_URL` secret
   exists.
3. `build-and-push` + `deploy-aws-ec2` run **only if** the AWS/Docker
   Hub secrets exist.

So: configure only Render's secret and only Render deploys; configure
only AWS's secrets and only AWS deploys; configure both and both
deploy independently from the same push. No workflow edits needed
either way.

## 6. Post-deploy checklist (either platform)

- [ ] `GET /healthz` returns `{"status":"ok"}`
- [ ] `GET /api/v1/health/` returns DB-connected status
- [ ] Sign up through the frontend (Supabase), then
      `python -m app.database.set_user_role <email> admin` to get
      admin access
- [ ] `python -m app.database.seed_real_data` for demo stations/trains
- [ ] Frontend's `NEXT_PUBLIC_API_URL` points at the deployed backend
      URL, redeployed on Vercel
- [ ] WebSocket (`/ws/monitor`) connects from the deployed frontend —
      check the browser console for connection errors, and that
      `CORS_ORIGINS` includes the exact frontend URL

## 7. Milestone 4 mapping

| Spec item | Status |
|---|---|
| Application testing and workflow validation | `tests/` (30+ files) already in repo; CI runs them on every push |
| UI responsiveness / optimization | Frontend responsibility — see frontend repo |
| Deploy platform using Docker and cloud environments | This guide — Render and/or AWS, both via the same `Dockerfile` |
| Final project documentation | This file + `docs/` folder already in the backend repo |
| Demonstrate the complete platform | Do a full walkthrough against the deployed URL once the checklist above is green |
