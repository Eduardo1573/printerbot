# Telegram Printer Bot for HP LaserJet MFP M129–M134

A Telegram bot running on a DigitalOcean VPS that:

* **prints** PDFs you send it on your home HP LaserJet (M129–M134 series), always **exactly one copy**;
* **delivers scans** from that printer to the Telegram user who pressed **📥 Receive Scan**.

There is no computer at home. The only infrastructure is the **VPS, the printer, the home router and Telegram**.

---

## Can this work without a computer at home?

Yes, **if your home router can run a WireGuard (VPN) client**. Full research with sources:
[docs/INVESTIGATION.md](docs/INVESTIGATION.md). Summary:

| Question | Answer (from HP's M129–M134 user guide / datasheet) |
|---|---|
| Can the printer connect out to a server or VPN? | **No.** It has no VPN client. HP ePrint (cloud e-mail printing) was shut down in 2023. |
| How can the VPS print to it? | Through **IPP/AirPrint (TCP 631)**, carried by a **WireGuard tunnel run by the home router**. Nothing is exposed to the Internet. |
| Can it print PDF directly? | **No.** A real M130fn only accepts URF (AirPrint raster) and PCLm. The bot converts PDF → **URF** with Ghostscript (driverless printing). PCLm is never used: it crashes this firmware. |
| How do scans reach the VPS? | **M130fw / M132fw:** the printer's **Scan to E-mail** sends to the bot's built-in SMTP receiver through the tunnel. **Other network models:** the panel can't send scans anywhere. The bot can try to *pull* scans over **eSCL**, which HP doesn't document for this series, so it **must be verified** on your unit with the probe tool. **"a" models:** USB-only, so this can't work. |

### What you need to know about your hardware

1. **The exact printer model**, from the front label or the configuration report: e.g. `M130fw`, `M132nw`, `M134fn`.
   It decides how scanning works:

   | Models | Printing | Scanning | `SCAN_MODE` |
   |---|---|---|---|
   | M130fw, M132fw (touchscreen) | ✅ IPP | ✅ Scan to E-mail (push, documented by HP) | `email` |
   | M130nw, M132nw, M132snw, M130fn, M132fn, M132fp, M133fn, M134fn | ✅ IPP | ⚠️ eSCL pull, **verify with probe**; otherwise `disabled` | `escl` / `disabled` |
   | M130a, M132a, M133a, M134a | ❌ USB only | ❌ | — (not possible without a computer) |

2. **The router make/model**, and whether it supports a **WireGuard client with access from the tunnel into the LAN**
   (OpenWrt, MikroTik RouterOS 7, GL.iNet, ASUS 388+/Merlin, FRITZ!Box 7.5+, Keenetic and UniFi do).
   If your ISP router can't, put a small WireGuard-capable router (e.g. GL.iNet) between it and the printer.

---

## Architecture

```text
                       Telegram
                          │  (HTTPS, long polling — no inbound port needed)
                          ▼
   ┌───────────────────────────────────────────────┐
   │ DigitalOcean VPS                              │
   │  docker compose:                              │
   │   bot ── aiogram handlers                     │
   │       ├─ PrintService ─ Ghostscript ─ IPP ────┼──┐
   │       ├─ ScanService (FIFO queue)             │  │
   │       │    └─ SMTP receiver :2525 ◄───────────┼──┼─┐
   │       └─ SQLite file (users, scan_requests,   │  │ │
   │                      print_jobs)              │  │ │
   │  wg0 10.8.0.1  ◄──── WireGuard UDP 51820 ─────┼──┘ │
   └───────────────────────────────────────────────┘    │
                          ▲ encrypted tunnel (router dials out; no ports opened at home)
   ┌──────────────────────┴────────────────────────┐    │
   │ Home router  wg 10.8.0.2                      │    │
   │  firewall: 10.8.0.1 → printer:631 only        │    │
   └──────────────────────┬────────────────────────┘    │
                          ▼                             │
   ┌───────────────────────────────────────────────┐    │
   │ HP LaserJet MFP M129–M134  192.168.1.50       │    │
   │  IPP :631 (print)  ·  Scan to E-mail ─────────┼────┘ (SMTP to 10.8.0.1:2525)
   └───────────────────────────────────────────────┘
```

* **Print:** Telegram PDF → validated → Ghostscript → URF raster → IPP `Print-Job` with `copies=1` → job is
  polled until it completes → the user gets the result.
* **Scan (email mode):** the user presses 📥 Receive Scan → a `pending` request is created. Someone presses
  Scan → E-mail on the printer → the printer sends the scan to the bot's SMTP receiver → the **oldest
  non-expired pending request** gets it → it's delivered to that user. If nobody is waiting, the bot replies
  `550` and the printer shows a send error. The scan is never delivered to anyone.
* **Scan (eSCL mode, e.g. M130fn):** the user loads a stack in the document feeder (or puts one page on the glass)
  and presses **📷 Scan** (or `/scan`). With paper in the feeder the whole stack is scanned, otherwise the glass
  (300 dpi, color). The bot then sends previews with **➕ Scan more**, **🔄 Rescan last page**,
  **📄 Make document** and **✖️ Cancel**. *Make document* merges all pages into one PDF. Only one session exists at a
  time (one scanner): others are told the scanner is busy. A session idle for `SCAN_SESSION_TIMEOUT` (10 min) is
  discarded and its owner is notified.

### Scan ownership rules (email mode)

* One active request per user (enforced with a DB partial unique index).
* Queue: `A → B → C` waiting means the next scan goes to A, then B, then C (FIFO, row-locked claim, so it's
  never ambiguous even if scans arrive at the same time).
* Requests expire after `SCAN_REQUEST_TIMEOUT` seconds. Expired, cancelled or logged-out requests never
  receive scans. Users are told when their request expires.
* If the bot restarts mid-delivery, the request is marked `failed`, never delivered twice.

---

## Installation from zero

Commands assume an **Ubuntu 24.04** droplet. Replace example values (`192.168.1.0/24`, `192.168.1.50`) with yours.

### 1. Create the Telegram bot with BotFather

1. In Telegram, open **@BotFather** → `/newbot`.
2. Choose a display name and a username ending in `bot`.

### 2. Get the bot token

BotFather replies with a token like `123456789:AA...`. Keep it secret. You'll put it in `.env`.
Optional: `/setprivacy` → *Enable*. The bot ignores groups anyway.

### 3. Prepare the DigitalOcean VPS

Create a droplet (Ubuntu 24.04, 1 GB RAM is enough), add your SSH key, then:

```bash
ssh root@YOUR_VPS_IP
apt update && apt upgrade -y
apt install -y git wireguard ufw
ufw allow OpenSSH
ufw allow 51820/udp        # WireGuard from your home router
ufw enable
```

No other inbound ports are needed: Telegram uses outbound long polling, and the SMTP receiver is
only reachable through the tunnel.

### 4. Install Docker

```bash
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt update
apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
docker compose version
```

### 5. Clone the repository

```bash
git clone https://github.com/YOUR_ACCOUNT/printerbot.git /opt/printerbot
cd /opt/printerbot
```

### 6. Create `.env`

```bash
cp .env.example .env
chmod 600 .env
# generate secrets:
echo "AUTH_CODE=$(openssl rand -base64 18)"
echo "SCAN_SMTP_PASSWORD=$(openssl rand -hex 16)"
nano .env
```

Set at least `TELEGRAM_BOT_TOKEN`, `AUTH_CODE`, `PRINTER_MODEL`, `PRINTER_HOST`,
`SCAN_MODE`, and `SCAN_SMTP_PASSWORD` (email mode). Every option is described in [`.env.example`](.env.example).

### 7. Configure the printer

Follow [Physical printer setup](#physical-printer-setup) below.

### 8. Configure networking (WireGuard VPS ⇄ home router)

On the VPS:

```bash
cd /opt/printerbot
sudo ./deploy/wireguard/setup-vps.sh 192.168.1.0/24      # your home LAN subnet
```

The script:
* creates `/etc/wireguard/wg0.conf` (VPS = `10.8.0.1`, router peer = `10.8.0.2`, home LAN routed through the tunnel)
  and starts `wg-quick@wg0`;
* adds a raw-table rule so traffic to `10.8.0.1` is only accepted when it arrives on `wg0`;
* makes Docker start **after** WireGuard (the SMTP port is bound to `10.8.0.1`);
* **prints the router's WireGuard config.** Enter it on your router as described in
  [Router configuration](#router-configuration).

Check the tunnel from the VPS:

```bash
wg show                                  # "latest handshake" should be seconds ago
ping -c 3 192.168.1.50                   # the printer (if your router allows ICMP)
curl -s -o /dev/null -w '%{http_code}\n' http://192.168.1.50:631/ipp/print   # any HTTP code = reachable
```

### 9. Run Docker Compose

```bash
cd /opt/printerbot
docker compose up -d --build
docker compose ps                # bot should become "healthy"
docker compose logs -f bot       # look for "bot_started" and "printer_reachable"
```

Migrations run automatically on start. Both services restart automatically (`restart: unless-stopped`).

Verify the printer over the real path (IPP formats, printer state, eSCL availability):

```bash
docker compose run --rm bot python -m app.tools.probe
```

### 10. Test authentication

1. Open your bot in Telegram → `/start` → *"🔐 Authentication required. Send the access code to continue."*
2. Send a wrong code → *"❌ Invalid access code."* (5 wrong codes lock that Telegram account for 15 min).
3. Send `AUTH_CODE` → *"✅ Authentication successful."* The message containing the code is deleted, and the
   **🖨 Print / 📥 Receive Scan** keyboard appears.
4. `/logout` removes your access. `/start` asks for the code again.

### 11. Test PDF printing

Send any PDF **as a file**. You'll see *"📄 Document received. 🖨 Printing..."*, then
*"✅ Printed successfully."* Exactly one page set comes out. Logs show `pdf_received`,
`print_job_started`, `pdf_rasterized`, `printer_accepted_job`, `print_job_completed`.

To test from the VPS without Telegram:

```bash
docker compose cp ./some.pdf bot:/tmp/test.pdf
docker compose exec bot python -m app.tools.probe --print /tmp/test.pdf
```

### 12. Test scan reception

**email mode (M130fw / M132fw):** press **📥 Receive Scan** → put a page on the glass → on the touchscreen choose
**Scan → E-mail** → choose the *Telegram bot* profile/contact → Start. You'll get *"📄 Scan received."* and the PDF.
Press Scan without anyone waiting and the printer shows a send error. That's intended.

**eSCL mode (M130fn):** put a page on the glass → **📷 Scan** → you get a preview of page 1 → swap the page →
**➕ Scan more** → … → **📄 Make document** → one PDF with all pages. **🔄 Rescan last page** replaces a bad page.

---

## Physical printer setup

Steps marked *(HP guide)* follow HP's user guide c05208327 for this series.

### Network connection

1. Connect the printer by **Ethernet** (most reliable) or Wi-Fi.
   * 2-line panels: **Setup → Network Setup → Show IP Address**. Touchscreen: **Connection Information** button. *(HP guide)*
2. Open the **Embedded Web Server (EWS)** from a phone/laptop on the home LAN: `http://PRINTER_IP` (accept the
   certificate warning). *(HP guide)*

### Fixed IP

The bot connects to `PRINTER_HOST`, so the printer's address must never change:

* **Recommended:** a **DHCP reservation** on the router for the printer's MAC address (shown on the network configuration page).
* Or a static IP on the panel (2-line models): **Setup → Network Setup → IPv4 Config Method → Manual**, or in EWS → **Networking**. *(HP guide)*

### EWS configuration

1. **Set an administrator password:** EWS → **System → Administration → Product Security** → Password. *(HP guide)*
2. **Update the firmware** (panel: **Setup → Service → LaserJet Update**, or HP's Firmware Update Utility). *(HP guide)*
   This matters for AirPrint/IPP behaviour.
3. Leave the network services at their defaults (EWS → **Networking**, which holds the IPv4/IPv6/Bonjour settings *(HP guide)*).
   The bot uses IPP on port 631, the AirPrint transport. `python -m app.tools.probe` confirms it answers.
4. **HP Web Services / ePrint:** not used. You can leave it off.
5. Turn off anything you don't use (e.g. Wi-Fi Direct) to reduce attack surface.

### Scan to E-mail (M130fw / M132fw, `SCAN_MODE=email`)

From *(HP guide)* "Method two: Set up the Scan to Email feature via the HP Embedded Web Server":

1. EWS → **System → Administration** → *Enabled Features* → **Scan to Email: On**.
2. EWS → **Scan → Outgoing E-mail Profiles → New**:
   * *Your E-mail Address:* `printer@printerbot.local` (any address. The bot doesn't relay mail.)
   * *Display Name:* `Telegram bot`
   * *SMTP Server:* `10.8.0.1`   *SMTP Port:* `2525`
   * *Always use secure connection (SSL/TLS):* **off** (traffic is already encrypted by WireGuard; see
     `SCAN_SMTP_TLS_MODE` if you need TLS)
   * *SMTP server requires authentication:* **on**. User ID = `SCAN_SMTP_USERNAME`, Password = `SCAN_SMTP_PASSWORD`
   * *Maximum Size for E-mail Message Attachments:* Automatic
   * Optional 4-digit **PIN** so only people who know it can send to the bot.
   * Click **Save and Test**. The bot accepts the test message and discards it.
3. EWS → **Scan → E-mail Address Book** → add contact `Telegram` → `scan@printerbot.local`.
4. EWS → **Scan → E-mail options**: *Scan File Type* **PDF**, resolution 300 dpi, Output Color as you like.

### eSCL (other network models, `SCAN_MODE=escl`)

No printer settings are needed beyond a working network connection. The endpoint is normally
`http://PRINTER_IP:8080/eSCL`. Verify with `python -m app.tools.probe --scan /tmp`. If it fails, scanning isn't
possible on that model without a computer: set `SCAN_MODE=disabled`.

### Authentication on the printer

IPP printing on this series has no per-job authentication. Access control comes from the network: only the VPS
(`10.8.0.1`) may reach the printer through the tunnel, and the router firewall allows only TCP 631 (+8080 for eSCL).
SMTP scans require the SMTP username/password, and optionally the PIN.

---

## Router configuration

Your router must:

1. Run a **WireGuard client** using the config printed by `setup-vps.sh` (`Address 10.8.0.2/32`,
   peer `AllowedIPs = 10.8.0.1/32`, `PersistentKeepalive = 25`). **Never use `0.0.0.0/0`**: that would route all
   your home Internet traffic through the VPS.
2. **Allow tunnel → printer:** source `10.8.0.1` → `PRINTER_IP` TCP **631** (and **8080** for eSCL). Deny
   everything else from the tunnel to the LAN and to the router itself.
3. **Allow printer → tunnel:** `PRINTER_IP` → `10.8.0.1` TCP **2525** (Scan to E-mail).
4. Ideally **don't masquerade** traffic into the tunnel. If your router does NAT it (common on consumer
   firmware), scans arrive from `10.8.0.2`, which is why `SCAN_SMTP_ALLOWED_NETWORKS` includes `10.8.0.2/32`.

No port forwarding is needed at home: the router dials out to the VPS.

Ready-made recipes:

* **OpenWrt:** [`deploy/wireguard/router-openwrt.sh`](deploy/wireguard/router-openwrt.sh)
* **MikroTik RouterOS 7:** [`deploy/wireguard/router-mikrotik.rsc`](deploy/wireguard/router-mikrotik.rsc)
* **GL.iNet / ASUS / FRITZ!Box / Keenetic / UniFi:** import the printed config as a *WireGuard client* (FRITZ!Box: *"connect networks"*).
  Then make sure the firmware **allows inbound connections from the VPN to the LAN**. Some "VPN client" modes block them, which breaks printing.

### VPS firewall summary

| Port | Exposure | Purpose |
|---|---|---|
| 22/tcp | Internet | SSH |
| 51820/udp | Internet | WireGuard (key-authenticated) |
| 2525/tcp | `10.8.0.1` only (tunnel) | Scan-to-E-mail receiver |

### Without a VPN-capable router: port forwarding (insecure, but works today)

Forwarding the router's TCP 631 to the printer, **restricted to the VPS IP**, technically works
(`PRINTER_HOST=your-home-public-ip`). But IPP sends your documents **unencrypted over the Internet**, and the
printer's embedded web stack wasn't designed to face the Internet. Many consumer routers can't restrict a
forward by source IP at all. Prefer a router that supports WireGuard. If you accept the risk anyway:

1. Check you are not behind CGNAT: the modem's WAN IP must equal your public IP (whatismyip.com).
2. Reserve the printer's IP (DHCP reservation) and set the EWS admin password.
3. Forward an unusual external port to the printer: `47631 → PRINTER_IP:631` (printing) and, only if you
   accept that anyone could trigger a scan, `47080 → PRINTER_IP:8080` (eSCL).
4. In `.env`: `PRINTER_HOST=<public IP or DDNS name>`, `PRINTER_PORT=47631`, `ESCL_PORT=47080`,
   `SMTP_BIND_IP=127.0.0.1`. Skip `setup-vps.sh`.
5. `docker compose up -d --build`, then `docker compose run --rm bot python -m app.tools.probe`.

Switching to the WireGuard setup later only changes `.env`.

---

## Configuration

All settings are in `.env` and documented in [`.env.example`](.env.example). The key limits:

| Variable | Default | Meaning |
|---|---|---|
| `MAX_PDF_SIZE_MB` | 20 | Max PDF size (Telegram Bot API's download limit is 20 MB) |
| `MAX_PDF_PAGES` | 100 | Max pages per PDF |
| `PRINT_TIMEOUT` | 120 (example: 300) | Seconds per job. A job that never started is cancelled. A job that is printing is left to finish. |
| `MAX_CONCURRENT_PRINT_JOBS` | 1 | Further PDFs wait in line |
| `SCAN_REQUEST_TIMEOUT` | 300 | Seconds a 📥 request waits for a scan |
| `MAX_SCAN_SIZE_MB` | 45 | Telegram bots can send ≤ 50 MB |

Copies are **not configurable**: every job is sent with `copies=1`, and the database rejects any other value
(`CHECK (copies = 1)`).

---

## Security

* Telegram: every protected handler goes through an auth middleware keyed on the **Telegram user ID**.
  Bots, group chats and invalid IDs are ignored. The access code is compared in constant time, **never
  stored**, never logged, and deleted from the chat. Brute force is limited per user.
* Files: the MIME type and extension are checked, the size is checked before and after download, the
  `%PDF-` header and a full parse are checked, encrypted PDFs are rejected, and pages are limited.
  Telegram filenames are never used. Each job gets a private `0700` temp dir that is always deleted.
* No shell: Ghostscript runs via `exec` with an argument list and `-dSAFER`.
* SQL only through the SQLAlchemy ORM (parameterised).
* Logs are structured JSON. A redaction filter scrubs the bot token, auth code and SMTP password, even
  inside tracebacks. Document contents and filenames are never logged.
* Containers: non-root user, read-only root filesystem, `cap_drop: ALL`, `no-new-privileges`.
* Network: the printer is never exposed to the Internet. The SMTP receiver only listens on the tunnel and
  accepts connections only from `SCAN_SMTP_ALLOWED_NETWORKS` with SMTP AUTH.

---

## Operations

```bash
docker compose logs -f bot                       # structured logs
git pull && docker compose up -d --build        # update
docker compose cp bot:/data/printerbot.db ./backup-$(date +%F).db   # back up the SQLite database
```

### Troubleshooting

| Symptom | Check |
|---|---|
| "Printer is unreachable" | `wg show` (handshake?), router firewall rule for 631, printer on/IP unchanged, `probe` |
| "Printer needs attention: out of paper / paper jam" | Reported by the printer itself via IPP |
| Bot container won't start after reboot | `systemctl status wg-quick@wg0`. Docker needs `10.8.0.1` to exist (set up by `setup-vps.sh`) |
| Printer shows e-mail send error | Nobody pressed 📥 Receive Scan (or it expired), or wrong SMTP credentials (`smtp_auth_failed` in logs), or source IP not in `SCAN_SMTP_ALLOWED_NETWORKS` (`smtp_connection_refused`) |
| Landscape pages printed small | Pages are scaled to fit the configured paper size (`PRINTER_PAPER_SIZE`) |

---

## Development and tests

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q                                   # SQLite, no printer needed

# full suite incl. real Ghostscript, in the production image:
docker build --target test -t printerbot:test . && docker run --rm printerbot:test

# same suite on PostgreSQL:
docker run --rm --network host -e TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost/db printerbot:test
```

The printer is never required. Tests use a fake IPP printer (an HTTP server speaking IPP), a fake eSCL
scanner, a real local SMTP session, and mocked Telegram objects. Covered: authentication, unauthorized
access, logout, PDF validation, size limits, print job creation, `copies=1` on the wire and in the DB,
scan requests, ownership/FIFO, concurrent scans, expiration, multiple waiting users, and printer
unreachable/busy/out of paper/timeout/aborted.

### Project structure

```text
app/
  main.py               startup, dispatcher, background tasks
  config.py             settings (.env)
  logging_setup.py      JSON logs + secret redaction
  healthcheck.py        Docker HEALTHCHECK
  database/             SQLAlchemy models + engine
  bot/                  aiogram handlers, keyboards, middleware, notifier, texts
  printer/
    base.py             PrintBackend / PushScanSource / PullScanSource interfaces
    hp.py               HP M129–M134 IPP print backend
    ipp.py              minimal IPP/2.0 client
    raster.py           PDF → URF / PWG raster (Ghostscript)
    smtp_receiver.py    Scan-to-E-mail receiver
    escl.py             eSCL pull scanner
    models.py           per-model capabilities (from HP's guide)
  services/             auth, print and scan-queue logic (printer- and Telegram-agnostic)
  tools/probe.py        verify the real printer over the tunnel
migrations/             Alembic
deploy/wireguard/       VPS setup script, OpenWrt and MikroTik recipes
docs/INVESTIGATION.md   capability research and sources
tests/
```
