# Deploying to a free-tier GCP VM

remindarr runs on one `e2-micro` in `us-west1`, behind Caddy, which gets a
Let's Encrypt certificate automatically and forwards **only** `/line/webhook`
to the app. Built from this repo's `Dockerfile` on the VM; no registry.

Files: [`deploy/gcp/compose.yml`](../deploy/gcp/compose.yml),
[`deploy/gcp/Caddyfile`](../deploy/gcp/Caddyfile).

On the VM:

| What | Where |
|---|---|
| Source (a `git archive` of one commit) | `/opt/remindarr` |
| Secrets (`root:root`, mode `0600`) | `/etc/remindarr/remindarr.env` |
| Public hostname for Caddy (not secret) | `/etc/remindarr/caddy.env` |
| SQLite database (uid 1000) | `/var/lib/remindarr/remindarr.db` |
| Certificates | Docker volume `remindarr_caddy-data` |

The billing account and budget IDs are left out of this public repo on purpose; `gcloud billing accounts list` and `gcloud billing budgets list --billing-account=<BILLING_ACCOUNT_ID>` show them.

Every `gcloud` command below assumes:

```bash
P=remindarr-73bcc9 Z=us-west1-b
```

## Cost model

Checked against the Cloud Billing Catalog (JPY, prices effective
2026-11-07) and Google's [Free Tier page](https://cloud.google.com/free/docs/free-cloud-features#compute):

| Resource | Free allowance | Beyond it |
|---|---|---|
| `e2-micro` in us-west1/us-central1/us-east1 | hours equal to the month's hours (one VM, always on) | E2 core ¥3.45/h + RAM ¥0.46/GiB·h |
| Boot disk, **`pd-standard`** | 30 GB-months | ¥6.33/GB-month |
| Boot disk, `pd-balanced` (the default!) | **none** | ¥15.82/GB-month |
| External IPv4 on a standard VM | first 720 h/month | ¥0.79/h |
| Static IP **not in use** | none | ¥1.58/h (≈ ¥1,150/month) |
| Egress from North America | 1 GB/month (not to China/Australia) | — |
| Snapshots | none | — |

What that means here:

- **Expected bill: ~¥0, at most about ¥19 in 31-day months.** 720 free IP
  hours is less than 744. LINE webhooks are tiny; egress stays far under 1 GB.
- The disk is 20 GB `pd-standard`. Never recreate it with the default type.
- The IP is **ephemeral**. It survives reboots but **changes on a stop/start**.
  To pin it, promote it in place (same address):
  `gcloud compute addresses create remindarr-ip --addresses=<IP> --region=us-west1 --project=$P`.
  Not done yet: no Google source confirms an *attached* static address bills
  like an ephemeral one. And a static address on a stopped VM is billed at
  the "not in use" rate above.
- A budget alert (¥1,000/month, this project only, at 50/90/100%) emails the
  billing admins. It counts free-tier and discount credits, so free usage
  shows as ¥0. Promotional and subscription credits (the AI Pro credit,
  presumably) are left out, so the alert fires before that credit is
  silently spent. **It only alerts; it does not stop anything.**

## Create (what was done, in order)

```bash
gcloud projects create remindarr-73bcc9 --name=remindarr
gcloud billing projects link $P --billing-account=<BILLING_ACCOUNT_ID>
gcloud services enable compute.googleapis.com iap.googleapis.com \
    billingbudgets.googleapis.com cloudbilling.googleapis.com --project=$P

# Firewall: the default network ships SSH/RDP/ICMP open to the world.
gcloud compute firewall-rules delete default-allow-rdp default-allow-icmp --project=$P
gcloud compute firewall-rules update default-allow-ssh --project=$P \
    --source-ranges=35.235.240.0/20          # IAP only
gcloud compute firewall-rules create allow-web --project=$P --network=default \
    --rules=tcp:80,tcp:443 --source-ranges=0.0.0.0/0 --target-tags=web

gcloud compute instances create remindarr --project=$P --zone=$Z \
    --machine-type=e2-micro --image-family=debian-13 --image-project=debian-cloud \
    --boot-disk-type=pd-standard --boot-disk-size=20GB --tags=web \
    --no-service-account --no-scopes \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring
```

Then, on the VM (`gcloud compute ssh remindarr --project=$P --zone=$Z --tunnel-through-iap`):

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile \
    && sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
sudo apt-get update && sudo apt-get install -y unattended-upgrades docker.io docker-compose git
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
sudo systemctl enable --now docker
sudo install -d -m 0750 -o 1000 -g 1000 /var/lib/remindarr
```

Docker comes from Debian's own packages, not Docker's repo, so
unattended-upgrades also applies Docker's security fixes. The 2 GB swap is
there because 1 GB of RAM is tight for an image build.

Secrets: put `REMINDARR_LINE_CHANNEL_SECRET`,
`REMINDARR_LINE_CHANNEL_ACCESS_TOKEN` and `REMINDARR_ALLOWED_USERS` in a local
file (mode 0600), copy it over IAP, and install it. Never commit it.

```bash
gcloud compute scp remindarr.env remindarr:/tmp/ --project=$P --zone=$Z --tunnel-through-iap
# on the VM:
sudo install -d -m 0755 /etc/remindarr
sudo install -m 0600 -o root -g root /tmp/remindarr.env /etc/remindarr/remindarr.env
shred -u /tmp/remindarr.env
echo 'REMINDARR_HOST=<hostname>' | sudo tee /etc/remindarr/caddy.env
```

`REMINDARR_DATABASE_URL` is set in `compose.yml`, so it does not go in the env
file.

## Update (deploy a new commit)

From the repo, on the commit you want:

```bash
git archive --format=tar.gz -o /tmp/remindarr-src.tar.gz HEAD
gcloud compute scp /tmp/remindarr-src.tar.gz remindarr:/tmp/ --project=$P --zone=$Z --tunnel-through-iap
gcloud compute ssh remindarr --project=$P --zone=$Z --tunnel-through-iap --command='
  set -e
  sudo rm -rf /opt/remindarr.new && sudo mkdir /opt/remindarr.new
  sudo tar -xzf /tmp/remindarr-src.tar.gz -C /opt/remindarr.new && rm /tmp/remindarr-src.tar.gz
  sudo rm -rf /opt/remindarr && sudo mv /opt/remindarr.new /opt/remindarr
  cd /opt/remindarr/deploy/gcp && sudo docker compose up -d --build'
```

The database and certificates live outside `/opt/remindarr`, so replacing the
source tree does not touch them. Changing only an env file needs
`sudo docker compose up -d --force-recreate` in `/opt/remindarr/deploy/gcp`.

## Logs and health

```bash
# on the VM, in /opt/remindarr/deploy/gcp
sudo docker compose ps
sudo docker compose logs -f remindarr       # look for line.listening, remindarr.ready
sudo docker compose logs caddy | grep -i certificate
```

From anywhere, with the secret in the environment (not on the command line):

```bash
REMINDARR_LINE_CHANNEL_SECRET=... uv run python scripts/line_webhook_check.py https://<hostname>/line/webhook
```

That should print 200 for the signed payload and 401 for the wrong signature.
Any path other than `/line/webhook` gets a 404 from Caddy.

Both containers are `restart: unless-stopped` and Docker is enabled on boot,
so a reboot brings everything back (verified 2026-11-08).

## Backup and restore

The database is one SQLite file. Take a consistent copy with SQLite's online
backup API, which is safe while the app is running:

```bash
gcloud compute ssh remindarr --project=$P --zone=$Z --tunnel-through-iap --command='
  cd /opt/remindarr/deploy/gcp && sudo docker compose exec -T remindarr python -c "
import sqlite3; s=sqlite3.connect(\"/data/remindarr.db\"); d=sqlite3.connect(\"/data/backup.db\"); s.backup(d); d.close()"'
gcloud compute scp remindarr:/var/lib/remindarr/backup.db ./remindarr-$(date +%F).db \
    --project=$P --zone=$Z --tunnel-through-iap
```

To restore: `sudo docker compose stop remindarr`, copy the file over
`/var/lib/remindarr/remindarr.db` (owned by uid 1000), remove any `-wal`/`-shm`
files next to it, and start it again.

Snapshots would work too, but they are not free.

## Switching to your own domain

The current hostname is `8-235-64-101.sslip.io`, which needs no domain. The
catch: sslip.io is not on the Public Suffix List, so every sslip.io user shares
one Let's Encrypt limit of 50 new certificates per 7 days. Renewals are exempt,
so this only bites on first issuance or if the `caddy-data` volume is lost.

To move to your own name:

1. Create a DNS `A` record for it pointing at the VM's IP (and, ideally,
   promote the IP to static first; see the cost model).
2. Wait until `dig +short <name>` returns the IP **before** starting Caddy.
   Failed validations count against Let's Encrypt's rate limits.
3. `echo 'REMINDARR_HOST=<name>' | sudo tee /etc/remindarr/caddy.env`, then
   `sudo docker compose up -d --force-recreate caddy`.
4. Paste `https://<name>/line/webhook` into the LINE Developers Console
   (Messaging API → Webhook URL) and press Verify.

## Teardown

```bash
gcloud projects delete remindarr-73bcc9
```

That deletes the VM, disk, IP, firewall rules and all data after a 30-day
recovery window. Delete the budget separately; it belongs to the billing
account, not the project:

```bash
gcloud billing budgets delete <BUDGET_ID> \
    --billing-account=<BILLING_ACCOUNT_ID> --billing-project=$P
```

Run that budget command **before** deleting the project, or pass another
project with the Budgets API enabled as `--billing-project`. Back up the
database first if you want to keep it.

## Known gaps

- Behind Caddy, `line.bad_signature` logs Caddy's container address as
  `remote`, not the caller's.
- No automated backups. The procedure above is manual.
