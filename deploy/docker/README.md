# Docker: build here, pull on the server

The image is built on a workstation and pushed to GitHub Container Registry
(GHCR). The prod server never sees the source code and needs no Node or Python:
it holds three files and pulls the image.

```
workstation                          GHCR (private)                 prod server
-----------                          --------------                 -----------
release.sh  -- build + push -->  ghcr.io/yashwant17-smit/      <-- deploy.sh pulls
                                   crb-statement-analyzer:<sha>           compose.yaml, .env
```

| File | Where it runs | Purpose |
|---|---|---|
| `release.sh` | workstation | Builds the `linux/amd64` image from the repo-root `Dockerfile` and pushes it tagged `:<commit>` and `:latest` |
| `compose.yaml` | server | Runs the pulled image on `127.0.0.1:8000` with a read-only filesystem and log rotation |
| `env.example` | server | Template for `.env` (password, intake token, origins) |
| `deploy.sh` | server | `docker compose pull && up -d`, then waits for the health check |

---

## One-time: workstation

1. Make sure Docker works without sudo:
   ```bash
   sudo usermod -aG docker $USER     # then log out and back in
   ```
2. Create a GitHub personal access token (classic) with **`write:packages`**:
   GitHub → Settings → Developer settings → Personal access tokens.
3. Log in:
   ```bash
   echo <token> | docker login ghcr.io -u yashwant17-SMIT --password-stdin
   ```

## Every release: workstation

```bash
git checkout main && git pull           # commit everything first; the script refuses a dirty tree
./deploy/docker/release.sh              # or: ./deploy/docker/release.sh v1.2.0
```

It prints the commit tag to deploy, e.g. `IMAGE_TAG=3f9c2ab ./deploy.sh`.

The first push creates the package as **private**. Keep it that way. You can check
under GitHub → your profile → Packages → crb-statement-analyzer → Package settings.

---

## One-time: prod server

1. Install Docker:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER     # then log out and back in
   ```
2. Log in to GHCR with a token that has **only `read:packages`** (make a
   separate token from the one on your workstation):
   ```bash
   echo <read-token> | docker login ghcr.io -u yashwant17-SMIT --password-stdin
   ```
3. Copy the three server files over:
   ```bash
   # from the workstation
   ssh user@server 'mkdir -p ~/crb-statement-analyzer'
   scp deploy/docker/{compose.yaml,deploy.sh,env.example} user@server:~/crb-statement-analyzer/
   ```
4. Fill in the secrets on the server:
   ```bash
   cd ~/crb-statement-analyzer
   cp env.example .env && chmod 600 .env
   nano .env           # ACCESS_PASSWORD is required; INTAKE_TOKEN only if Apps Script posts here
   ```
5. Put HTTPS in front of it. The container listens on `127.0.0.1:8000`, which is
   what [`../linux/nginx.conf.example`](../linux/nginx.conf.example) proxies to:
   ```bash
   sudo apt install -y nginx certbot python3-certbot-nginx
   sudo cp nginx.conf.example /etc/nginx/sites-available/crb-statement-analyzer   # scp it over too
   sudo nano /etc/nginx/sites-available/crb-statement-analyzer                    # set server_name
   sudo ln -s /etc/nginx/sites-available/crb-statement-analyzer /etc/nginx/sites-enabled/
   sudo rm -f /etc/nginx/sites-enabled/default
   sudo nginx -t && sudo systemctl reload nginx
   sudo certbot --nginx -d underwriting.yourdomain.com --redirect
   sudo ufw allow OpenSSH && sudo ufw allow 'Nginx Full' && sudo ufw enable
   ```
   Your domain needs a DNS A record pointing at the server first, or certbot
   fails. Then set `ALLOWED_ORIGINS` in `.env` and the Apps Script `LEDGER_URL`
   to the same `https://` address. The full walkthrough, with checks, is in
   [`../linux/README.md`](../linux/README.md#option-b-docker).

## Every release: prod server

```bash
cd ~/crb-statement-analyzer
./deploy.sh 3f9c2ab        # the tag release.sh printed; no argument = IMAGE_TAG in .env, else latest
```

Deploying by commit tag rather than `latest` means the server always knows
exactly which code it is running.

## Operations

| Task | Command (in `~/crb-statement-analyzer`) |
|---|---|
| Roll back | `./deploy.sh <previous-sha>` |
| Which version is live | `docker inspect -f '{{.Config.Image}}' crb-statement-analyzer` |
| Logs | `docker compose logs -f` |
| Apply an edited `.env` | `docker compose up -d` (a plain `restart` does not re-read it) |
| Stop | `docker compose down` |
| Free disk from old images | `docker image prune -a` |

The container restarts on crash and at boot (`restart: unless-stopped`).
Docker itself must be enabled at boot: `sudo systemctl enable docker`.

---

## No registry? Ship the image as a file

```bash
# workstation
PUSH=0 ./deploy/docker/release.sh
docker save ghcr.io/yashwant17-smit/crb-statement-analyzer:latest | gzip > crb-statement-analyzer.tar.gz
scp crb-statement-analyzer.tar.gz user@server:~/crb-statement-analyzer/

# server
docker load < crb-statement-analyzer.tar.gz
docker compose up -d       # skip deploy.sh: it would try to pull
```
