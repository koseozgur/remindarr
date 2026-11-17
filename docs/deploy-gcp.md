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
- The IP is **ephemeral**. It survives reboots but **changes on a stop/start**,
  and the sslip.io hostname is the IP, so a stop/start breaks the LINE
  webhook (this happened on 2026-11-17). After one: put the new
  `<a-b-c-d>.sslip.io` in `/etc/remindarr/caddy.env`, recreate Caddy, and
  update the Webhook URL in the LINE console. Moving to your own domain only
  shrinks this to a DNS edit.
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

**Once, for the deploy that adds the pinned network** (`172.28.0.0/24` in
`compose.yml`, which `REMINDARR_TRUSTED_PROXIES` names): run
`sudo docker compose down` before `up`, so the network is recreated with that
subnet. It is untested on the VM. If the subnet clashes with something there,
change both values together.

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

`https://<hostname>/healthz` answers 200 while the scheduler is ticking and the
newest backup is under 49 hours old, and 503 with a `problems` list otherwise
(also logged as `health.unhealthy`). The scheduler only counts ticks that
succeeded, so a broken database shows up as "scheduler is not ticking". Any
other path gets a 404 from Caddy.

The body also has `"ai": "ok" | "degraded" | "off"`. A rejected AI
credential shows as `degraded` but **never** turns the check into a 503: the
bot still works without AI, so an expired key shouldn't page anyone.

Both containers are `restart: unless-stopped` and Docker is enabled on boot,
so a reboot brings everything back (verified 2026-11-08).

## Admin notices

Every 30 minutes (`REMINDARR_AI_WATCH_MINUTES`) the app checks two things that
would otherwise fail quietly, and messages every admin once when one goes
wrong. The message arrives like a reminder, on the admin's `/deliver` app (or
any app but LINE, which is metered), starting with "Admin notice:".

- **AI credentials.** A free call (listing models) tests the key. When Claude
  rejects it, admins hear about it once, and again only after it has worked
  in between. Logs: `ai.bad_key`, then `watch.notice`.
- **LINE push quota.** At 80% of the monthly limit
  (`REMINDARR_LINE_QUOTA_WARN_PERCENT`), once a month. LINE calls its usage
  figure approximate. Log: `line.quota` on every check.

Both are remembered in memory, so a restart while a problem persists sends the
notice once more.

## Rotating the Claude API key

The key is a 30-day key; the current one expires **2026-12-17**. The keyless
setup below makes this whole section unnecessary.

1. In the Claude Console (platform.claude.com → API keys), create a new key
   with an expiry. Keep the old one for now.
2. On the dev Mac, write the line to a file nobody else can read, and copy it
   over IAP. The key never goes on a command line:

   ```bash
   umask 077; printf 'REMINDARR_ANTHROPIC_API_KEY=%s\n' "$(pbpaste)" > /tmp/anthropic.env
   gcloud compute scp /tmp/anthropic.env remindarr:/tmp/ --project=$P --zone=$Z --tunnel-through-iap
   rm /tmp/anthropic.env
   ```

3. On the VM, swap that one line in the env file and restart the app:

   ```bash
   sudo sh -c 'umask 077; grep -v "^REMINDARR_ANTHROPIC_API_KEY=" /etc/remindarr/remindarr.env \
       > /etc/remindarr/remindarr.env.new && cat /tmp/anthropic.env >> /etc/remindarr/remindarr.env.new \
       && mv /etc/remindarr/remindarr.env.new /etc/remindarr/remindarr.env'
   shred -u /tmp/anthropic.env
   cd /opt/remindarr/deploy/gcp && sudo docker compose up -d --force-recreate remindarr
   ```

4. Check: `sudo docker compose logs remindarr | grep ai.` shows `ai.enabled`
   and no `ai.bad_key`; `/healthz` says `"ai": "ok"` (the first check runs at
   startup).
5. Delete the old key in the Console.

These exact commands haven't been run yet. The first rotation is their test.

## Keyless AI: workload identity federation

The Claude API accepts a Google-signed identity token from the VM in place of
an API key (Anthropic's docs: *Workload Identity Federation*, and *Use WIF
with Google Cloud*). The VM swaps its own identity token for a short-lived
Anthropic token, and the SDK refreshes it. Nothing is stored, and nothing
expires on a calendar. The app supports it (`anthropic` 2.9.0's
`WorkloadIdentityCredentials`), but **it has not been run on the VM yet.**
Untested so far: that the container reaches the metadata server at
169.254.169.254, and the Console setup itself.

The docs require a user-managed service account attached to the VM, not the
Compute Engine default. `remindarr-backup` is one. Fetching its identity token
needs no extra IAM role. The federation rule pins it by numeric id.

1. Get the service account's numeric id:

   ```bash
   gcloud iam service-accounts describe remindarr-backup@$P.iam.gserviceaccount.com \
       --format='value(uniqueId)'
   ```

2. In the Claude Console, go to Settings → Workload identity federation →
   Connect workload → Google Cloud. The issuer is `https://accounts.google.com`
   (discovery). The rule should match audience `https://api.anthropic.com`
   **and** both claims, `sub` (the numeric id) and `email` (the account's
   email). Use scope `workspace:developer`. Never use a `subject_prefix` for
   Google tokens: per the docs, a wildcard matches service accounts in every
   project. Note the rule id (`fdrl_…`), the organization id, and the
   Anthropic service account id (`svac_…`).
3. In `/etc/remindarr/remindarr.env`, **remove** `REMINDARR_ANTHROPIC_API_KEY`
   (config refuses both at once) and add:

   ```
   REMINDARR_ANTHROPIC_FEDERATION_RULE_ID=fdrl_...
   REMINDARR_ANTHROPIC_ORGANIZATION_ID=<uuid>
   REMINDARR_ANTHROPIC_SERVICE_ACCOUNT_ID=svac_...
   # REMINDARR_ANTHROPIC_WORKSPACE_ID=wrkspc_...   only if the rule covers several
   ```

   None of these are secrets, but they go in the same file anyway.
4. `sudo docker compose up -d --force-recreate remindarr`, then check that the
   logs show `ai.enabled ... auth=federation` and no `ai.bad_key`, and that
   `/healthz` says `"ai": "ok"`. If the exchange is denied, the reason is on
   the Console's authentication history page. The API's own answer is a bare
   401.
5. Delete the API key in the Console.

## Backup and restore

The app backs itself up: at start if the newest copy is over a day old, then
daily. Each copy is taken with SQLite's online-backup API (safe while running),
passes `PRAGMA integrity_check` before it gets its final name, and the newest
14 are kept, in `/var/lib/remindarr/backups/remindarr-<UTC time>.db` on the VM.
Look for `backup.done` / `backup.failed` in the logs.

Those copies share the VM's disk, so they cover a bad deploy or a mistaken
`/remove`, not losing the disk. To pull the newest one to your machine:

```bash
gcloud compute ssh remindarr --project=$P --zone=$Z --tunnel-through-iap \
    --command='ls -1 /var/lib/remindarr/backups | tail -1'
gcloud compute scp remindarr:/var/lib/remindarr/backups/<name> . \
    --project=$P --zone=$Z --tunnel-through-iap
```

### Off the VM: GCS

Set up 2026-11-17. The bucket `gs://$P-backups` is in us-west1 (Always Free:
5 GB-months, 5,000 uploads a month; this uses about 30 and well under 1 MB).
It has uniform access, public access prevention enforced, and a lifecycle
rule that deletes objects after 30 days.

The VM runs as the service account `remindarr-backup@$P.iam.gserviceaccount.com`,
which holds exactly one role, `roles/storage.objectCreator`, on that bucket
only. It can add a backup, but it can't list, read, overwrite or delete one,
so a compromised VM can't wipe the off-site copies. There is no key file: the
VM gets short-lived tokens from the metadata server. Attaching it needed a VM
stop/start (see the IP warning in the cost model).

The host runs `deploy/gcp/backup-upload/` hourly, and it uploads only when the
app has written a new copy. Install or update it:

```bash
# on the VM
echo "REMINDARR_BACKUP_BUCKET=$P-backups" | sudo tee /etc/remindarr/backup.env
cd /opt/remindarr/deploy/gcp/backup-upload
sudo install -m 0755 remindarr-backup-upload.sh /usr/local/bin/remindarr-backup-upload
sudo install -m 0644 remindarr-backup-upload.service remindarr-backup-upload.timer /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now remindarr-backup-upload.timer
sudo systemctl start remindarr-backup-upload.service && journalctl -u remindarr-backup-upload -n 5
```

To restore from GCS, from your machine (your account can read the bucket):

```bash
gcloud storage ls gs://$P-backups/
gcloud storage cp gs://$P-backups/<name> .
```

then copy the file to the VM and restore it as below.

A one-off copy by hand, outside the schedule:

```bash
gcloud compute ssh remindarr --project=$P --zone=$Z --tunnel-through-iap --command='
  cd /opt/remindarr/deploy/gcp && sudo docker compose exec -T remindarr python -c "
import sqlite3; s=sqlite3.connect(\"/data/remindarr.db\"); d=sqlite3.connect(\"/data/backup.db\"); s.backup(d); d.close()"'
gcloud compute scp remindarr:/var/lib/remindarr/backup.db ./remindarr-$(date +%F).db \
    --project=$P --zone=$Z --tunnel-through-iap
```

To restore: `sudo docker compose stop remindarr`, copy a backup over
`/var/lib/remindarr/remindarr.db` (owned by uid 1000), remove any `-wal`/`-shm`
files next to it, and start it again.

Snapshots would work too, but they are not free.

## Switching to your own domain

The current hostname is `34-127-102-53.sslip.io` (since 2026-11-17; it was
`8-235-64-101` until a stop/start changed the IP), which needs no domain. The
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

- No alert yet. Planned: a Cloud Monitoring uptime check on `/healthz` with
  an email alert.
