# 🚀 Deployment, Configuration & Operations Guide — ScamTrap AI

This guide covers local environment setup, environment variable configuration, reverse proxy/tunneling setup, and production deployment best practices for ScamTrap AI.

---

## 1. System Requirements & Prerequisites

### Hardware Requirements
* **CPU:** 2 Cores minimum (4 Cores recommended for concurrent image & PDF rendering)
* **RAM:** 4 GB minimum (8 GB recommended for Chromium Puppeteer instance)
* **Disk:** 500 MB free storage

### Software Dependencies
* **Operating System:** Windows 10/11, macOS (Apple Silicon / Intel), or Linux (Ubuntu 20.04+, Debian 11+)
* **Python:** Version `3.10` or higher
* **Node.js:** Version `18.x` or `20.x LTS`
* **Google Chrome / Chromium:** Required for `whatsapp-web.js` browser automation.

---

## 2. Environment Variables Configuration (`.env`)

Create a `.env` file in the project root. Below is the complete configuration matrix:

```env
# =====================================================================
# 1. AI MODEL PROVIDER CREDENTIALS
# =====================================================================

# Primary: Groq API Key (High-speed Llama 3.3 70B inference)
GROQ_API_KEY="gsk_..."
GROQ_API_KEY_2=""

# Secondary: OpenRouter API Key
OPENROUTER_API_KEY=""

# Tertiary: Google Gemini API Keys (Free tier multi-bucket rotation)
GEMINI_API_KEY=""
GEMINI_API_KEY_2=""

# Model Priority Chain (comma-separated execution order)
MODEL_PRIORITY=groq,openrouter,pollinations,gemini

# Seconds to wait for an LLM response before falling back to tactical engine
GEMINI_TIMEOUT_S=20

# =====================================================================
# 2. PUBLIC TUNNELING (FOR CANARY IP EXTRACTION)
# =====================================================================

# Public HTTPS URL reachable by the scammer's phone.
# If left blank and cloudflared.exe is present, ScamTrap auto-spawns a free Quick Tunnel.
PUBLIC_TUNNEL_URL=""

# =====================================================================
# 3. SECURITY & SERVER BINDING
# =====================================================================

# Secret API key protecting /api/* from remote callers.
# Auto-generated on first startup if left blank.
SCAMTRAP_API_KEY=pUvsYUQGak4ZlfvzR9ma4MuAQa-NRRFv

# Bind Address: Use 127.0.0.1 for local isolation, or 0.0.0.0 for LAN exposure
HOST=127.0.0.1
PORT=8000

# Backend Base URL for Node.js bridge
BACKEND_BASE=http://localhost:8000
```

---

## 3. Launching ScamTrap AI

### 3.1 1-Click Automated Launcher (Recommended)
Run the built-in process manager which creates Python `.venv`, installs requirements, handles Node modules, cleans occupied ports, and launches all microservices:

```bash
python run.py
```

### 3.2 Manual Microservice Startup
If you prefer running components in separate terminal windows:

#### Terminal 1 — Python FastAPI Backend:
```bash
# Windows
.venv\Scripts\activate
python app.py

# Linux / macOS
source .venv/bin/activate
python app.py
```

#### Terminal 2 — Node.js WhatsApp Bridge:
```bash
npm install
node wa_bridge.js
```

---

## 4. Public Tunnel & Canary URL Setup

For the **Canary IP Tracking Links** to work when opened on a scammer's mobile phone, the endpoint `/receipt/{id}` must be reachable over the public internet (HTTPS).

### Method A: Built-in Cloudflare Quick Tunnel (Automatic)
* Place `cloudflared.exe` (or `cloudflared` on Linux) in the project directory.
* `app.py` will automatically start a free Quick Tunnel and adopt the generated `https://*.trycloudflare.com` URL.

### Method B: Manual ngrok or Named Tunnel
If you have your own ngrok or Cloudflare Named Tunnel:
```bash
ngrok http 8000
```
Copy the generated `https://xxxx.ngrok-free.app` and set it in your `.env`:
```env
PUBLIC_TUNNEL_URL="https://xxxx.ngrok-free.app"
```

---

## 5. Linux / Server Headless Deployment

When deploying to a headless Linux VPS (e.g. AWS EC2, DigitalOcean, Hetzner):

### 1. Install System Chromium & Font Libraries:
```bash
sudo apt update && sudo apt install -y \
    chromium-browser \
    libnss3 \
    libatk-bridge2.0-0 \
    libgtk-3-0 \
    libasound2 \
    fonts-liberation \
    fonts-dejavu-core
```

### 2. Configure Systemd Service (`/etc/systemd/system/scamtrap.service`):
```ini
[Unit]
Description=ScamTrap AI Autonomous Honeypot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/ScamTrap
ExecStart=/usr/bin/python3 /home/ubuntu/ScamTrap/run.py
Restart=always
RestartSec=5
Environment=HOST=0.0.0.0
Environment=PORT=8000

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now scamtrap
```

---

## 6. Troubleshooting & Common Issues

| Issue | Root Cause | Solution |
| :--- | :--- | :--- |
| **`[WinError 10048] Address already in use`** | A lingering process is holding port 8000. | `python run.py` automatically terminates stale processes. Or run: `netstat -ano \| findstr :8000` and `taskkill /F /PID <pid>`. |
| **`Puppeteer evaluation context destroyed`** | WhatsApp Web refreshed internal frame. | Handled automatically by `wa_bridge.js` error recovery listeners. No action needed. |
| **`429 Rate Limit Exceeded on LLM`** | Free tier provider reached RPM cap. | Handled automatically. The router applies exponential cooldowns and fails over to Groq/OpenRouter/Pollinations. |
| **`QR Code not scanning`** | High latency or browser zoom level. | Scan the large QR code rendered on the dashboard at `http://localhost:8000` rather than the terminal. |
| **`Canary link returns connection refused`** | `PUBLIC_TUNNEL_URL` not configured. | Ensure `cloudflared.exe` is present or configure an active ngrok/Cloudflare tunnel in `.env`. |
