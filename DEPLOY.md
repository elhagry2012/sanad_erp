# Deploying Sanad ERP to the web

## 1. Configure (no secrets in code)
Copy `.env.example` to `.env` (or set the same variables in your host's dashboard). Never commit `.env`.
Key variables: `SANAD_ENV`, `SANAD_DB_PATH`, `SANAD_BACKUP_DIR`, `SANAD_IMAGES_DIR`, `SANAD_ADMIN_PASSWORD`.

## 2. First start
- Fresh install: the admin account `momar` is created once. If `SANAD_ADMIN_PASSWORD` is empty, a random password is
  printed to the server log (look for `[SANAD] Initial admin password`). Either way it must be changed at first login.
- Existing `sanad.db`: put it at `SANAD_DB_PATH`. It is upgraded in place (columns/tables are only added). Weak legacy
  passwords (including the old 6-digit one) are forced to change at next login.

## 3. Run
Docker:
    docker build -t sanad-erp .
    docker run -d --name sanad -p 8501:8501 --env-file .env -v sanad_data:/data sanad-erp
Without Docker: `pip install -r requirements.txt && streamlit run app.py`

## 4. HTTPS is mandatory
Streamlit does not terminate TLS. Put it behind a reverse proxy (Caddy, nginx, a cloud load balancer) with HTTPS and
WebSocket support. Caddy example:
    sanad.example.com {
        reverse_proxy localhost:8501
    }
Keep the container port private; expose only the proxy.

## 5. Data safety
- One running instance only (SQLite is single-node). Write transactions are serialized (BEGIN IMMEDIATE).
- Put `/data` on a persistent disk. Daily automatic backups go to `SANAD_BACKUP_DIR`; scheduled disk snapshots are still recommended.
- Invoice images are files in `SANAD_IMAGES_DIR` (not inside the .db). Back them up together.
- If you outgrow one instance or need multi-region, migrate to PostgreSQL (the service layer isolates all SQL in `utils/`).

## 6. Security checklist
- [ ] HTTPS in front, port 8501 not public
- [ ] `SANAD_ENV=production`, strong admin password changed at first login
- [ ] `.env`, `*.db`, `backups/`, `invoice_images/` not in git/images (see `.gitignore`, `.dockerignore`)
- [ ] Review `audit_log` and the "Security log" tab regularly
- [ ] Limit each user's modules/restaurants in "Users & permissions"

## 7. Import / export notes
- PDF export needs no system packages: ReportLab (pure Python wheel) + the bundled font in `assets/fonts/` (make sure it is deployed with the code).
- Uploads for bulk import are limited to 5 MB / 5,000 rows, `.xlsx` or `.csv` only; macro workbooks and zip-bombs are rejected.
- The "Data import" module is sensitive: grant it per user in "Users & permissions". Every import and export is written to `audit_log`.
