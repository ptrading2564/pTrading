# pTrading VPS Deployment Guide (DigitalOcean Ubuntu 24.04 LTS)

Follow these steps to deploy pTrading to your dedicated DigitalOcean Droplet with automatic Let's Encrypt HTTPS via Caddy and containerized Docker Compose.

---

## 1. Droplet Creation & Initial Security

1. **Create Droplet on DigitalOcean**:
   - Image: **Ubuntu 24.04 LTS** (x64).
   - Size: Basic Regular SSD **1 vCPU / 1 GB RAM ($6/mo)** or **1 vCPU / 2 GB RAM ($12/mo)**.
   - Region: `nyc1` / `nyc3` (fastest latency to Alpaca's US-East servers) or `lon1`.
   - Authentication: **SSH Key** (recommended).

2. **Configure Firewall (`ufw`)**:
   SSH into your Droplet as `root`:
   ```bash
   ssh root@<YOUR_DROPLET_IP>
   ```
   Apply strict firewall rules allowing only SSH, HTTP, and HTTPS:
   ```bash
   ufw default deny incoming
   ufw default allow outgoing
   ufw allow 22/tcp
   ufw allow 80/tcp
   ufw allow 443/tcp
   ufw enable
   ```

---

## 2. Install Docker & Caddy

1. **Install Docker Engine & Compose plugin**:
   ```bash
   apt-get update
   apt-get install -y ca-certificates curl gnupg
   install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
   chmod a+r /etc/apt/keyrings/docker.gpg

   echo \
     "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
     $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
     tee /etc/apt/sources.list.d/docker.list > /dev/null

   apt-get update
   apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
   systemctl enable --now docker
   ```

2. **Install Caddy Reverse Proxy**:
   ```bash
   apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
   apt-get update
   apt-get install -y caddy
   systemctl enable --now caddy
   ```

---

## 3. DNS Configuration

In your DNS provider (Cloudflare, Namecheap, GoDaddy, DigitalOcean DNS, etc.):
- Add an **A Record**:
  - Name / Host: `@` (or `trading` or `app`)
  - Value / Target: `<YOUR_DROPLET_IP>`
  - TTL: Auto / 300 seconds
  - *Important:* If using Cloudflare, set proxy status to **DNS Only (Grey Cloud)** so TradingView webhook source IPs reach your server directly for IP allowlisting.

---

## 4. Deploy Application Code (`/opt/ptrading`)

1. **Create deployment directory**:
   ```bash
   mkdir -p /opt/ptrading/data /var/log/caddy
   cd /opt/ptrading
   ```

2. **Clone or Copy your project files into `/opt/ptrading`**:
   Ensure the following files are present:
   - `Dockerfile`
   - `docker-compose.yml`
   - `pyproject.toml`
   - `app/`
   - `deploy/Caddyfile`

3. **Configure Environment Variables (`.env`)**:
   ```bash
   cp .env.example .env
   nano .env
   ```
   Fill in your actual production values:
   - `DOMAIN_NAME`: Your configured domain (e.g. `trading.yourdomain.com`)
   - `WEBHOOK_SECRET`: A strong secret password
   - `DASHBOARD_PASSWORD`: Secure dashboard login password
   - `ALPACA_API_KEY` & `ALPACA_SECRET_KEY`: Your Alpaca Paper API keys
   - `TELEGRAM_BOT_TOKEN` & `TELEGRAM_CHAT_ID`: For mobile push alerts

---

## 5. Configure Caddy & Launch Stack

1. **Link Caddyfile**:
   ```bash
   cp /opt/ptrading/deploy/Caddyfile /etc/caddy/Caddyfile
   # Replace {$DOMAIN_NAME} with your actual domain if not using env vars
   sed -i "s/trading.yourdomain.com/<YOUR_ACTUAL_DOMAIN>/g" /etc/caddy/Caddyfile
   systemctl reload caddy
   ```

2. **Build and Start Docker Container**:
   ```bash
   cd /opt/ptrading
   docker compose up -d --build
   ```

3. **Verify Status & Logs**:
   ```bash
   docker compose ps
   docker compose logs -f
   ```

4. **Verify Health Endpoint**:
   ```bash
   curl -I https://<YOUR_DOMAIN>/health
   ```
   You should see `HTTP/2 200` with automated Let's Encrypt TLS certificate active!

---

## 6. Daily Operations & Commands

- **Check Application Logs**:
  ```bash
  cd /opt/ptrading && docker compose logs -f ptrading-app
  ```
- **Restart Application**:
  ```bash
  cd /opt/ptrading && docker compose restart
  ```
- **Database Backup**:
  SQLite WAL database is safely stored on the host at `/opt/ptrading/data/ptrading.db`. You can copy or snapshot this file anytime:
  ```bash
  cp /opt/ptrading/data/ptrading.db /opt/ptrading/data/ptrading_backup_$(date +%F).db
  ```
