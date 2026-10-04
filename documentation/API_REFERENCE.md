# 📡 REST API Reference & Protocols — ScamTrap AI

This document details all API endpoints, request/response schemas, query parameters, webhooks, and authentication protocols exposed by ScamTrap AI.

---

## 1. Authentication & Security

All `/api/*` endpoints are protected against unauthorized external access.

* **Local Loopback Calls:** Requests originating directly from `127.0.0.1`, `::1`, or `localhost` (such as the local Node.js bridge and local browser) are trusted without an API key.
* **Remote / Proxied Calls:** Requests arriving via Cloudflare Tunnels, ngrok, or LAN proxies must provide the secret `SCAMTRAP_API_KEY` in one of three ways:
  1. Header: `X-API-Key: <SCAMTRAP_API_KEY>`
  2. Header: `Authorization: Bearer <SCAMTRAP_API_KEY>`
  3. Query Param: `?api_key=<SCAMTRAP_API_KEY>`

---

## 2. Inbound Webhooks & Bridge Endpoints

### 2.1 Forward Inbound Message
Forward an incoming suspect WhatsApp message from the Node bridge to the backend agent.

* **Endpoint:** `POST /api/wa-incoming`
* **Content-Type:** `application/json`

#### Request Body Schema:
```json
{
  "msg_id": "true_919876543210@c.us_3EB0...",
  "sender_jid": "919876543210@c.us",
  "sender_number": "919876543210",
  "text": "Electricity Dept: Power disconnection tonight at 9:30 PM. Pay Rs 1500 immediately to electricity.discom@sbi",
  "platform": "whatsapp"
}
```

#### Response (200 OK — Active Trap):
```json
{
  "status": "TRAPPED",
  "should_reply": true,
  "reply_text": "Bhai main try kar raha hu par limit exceed ho gaya. Ye dekh screenshot aur dusra account bhej.",
  "media_base64": "iVBORw0KGgoAAAANSUhEUgAAA...",
  "trap_url": "https://greene-jean.trycloudflare.com/receipt/TXN-8841F2",
  "bot_report": "🎯 [TRAP ENGAGED] Suspect +919876543210 replied (IOCs: UPI ID: electricity.discom@sbi). AI deployed bait reply (₹1,500).",
  "is_media": true,
  "delivery_plan": [
    { "type": "wait", "duration_ms": 4200 },
    { "type": "typing" },
    { "type": "send_media", "caption": "Ye dekh payment fail ho gayi" }
  ],
  "recommended_delay_ms": 4200,
  "model_ms": 780,
  "total_delay_ms": 4980
}
```

---

### 2.2 Update WhatsApp Connection Status
Updates the global connection state reported by `wa_bridge.js`.

* **Endpoint:** `POST /api/wa-status`
* **Content-Type:** `application/json`

#### Request Body Schema:
```json
{
  "status": "CONNECTED",
  "phone": "919876543210",
  "my_jid": "919876543210@c.us",
  "qr": ""
}
```
*Possible `status` values:* `DISCONNECTED`, `QR_READY`, `AUTHENTICATED`, `CONNECTED`.

---

### 2.3 Outbox Queue Dispatch
Polled by `wa_bridge.js` to dispatch queued bot alerts or autonomous proactive replies.

* **Endpoint:** `GET /api/wa-outbox`
* **Response (200 OK):**
```json
{
  "queue": [
    {
      "target_jid": "919876543210@c.us",
      "text": "Bhai alternate account bhej jaldi.",
      "media_base64": null,
      "bot_report": "🎯 Live Alert on Phone 1"
    }
  ]
}
```

---

## 3. Telemetry & Control Endpoints

### 3.1 Get System State
Returns the complete real-time forensic state for the SOC Dashboard.

* **Endpoint:** `GET /api/state`
* **Response (200 OK):**
```json
{
  "case_id": "NCRP-CYBER-20261004-782914",
  "server_time_ms": 1728038400000,
  "wa_status": "CONNECTED",
  "wa_phone": "919876543210",
  "target_scammer": "+919876543210",
  "target_mode": "TRAP",
  "trapped_numbers": ["919876543210"],
  "monitored_numbers": [],
  "extracted_intel": {
    "upi_ids": ["electricity.discom@sbi"],
    "phone_numbers": ["919876543210"],
    "bank_accounts": ["98765432109876"],
    "ifsc_codes": ["SBIN0001234"]
  },
  "canary_hits": [
    {
      "receipt_id": "TXN-8841F2",
      "ip": "103.21.244.2",
      "isp": "Bharti Airtel Ltd",
      "os_device": "Android 14 / Chrome Mobile",
      "location": "Jaipur, Rajasthan, India",
      "lat": 26.9124,
      "lon": 75.7873,
      "timestamp": "04-10-2026 15:30:12"
    }
  ],
  "funnel": {
    "stage": 3,
    "stages": ["APPROACH", "RAPPORT", "PRESSURE", "PAYMENT_ASK", "BLOCKED"],
    "payment_asks": 2,
    "blocked": 1,
    "money_stalled": 1500
  },
  "dossier": {
    "playbook": "Electricity Bill Scam",
    "language": "Hinglish",
    "risk_score": 85,
    "risk_label": "CRITICAL",
    "aggression_label": "Elevated",
    "turns": 6
  }
}
```

---

### 3.2 Mode Switcher
Controls the surveillance mode for a specific suspect phone number.

* **Endpoint:** `POST /api/set-mode`
* **Request Body:**
```json
{
  "phone_number": "+919876543210",
  "mode": "TRAP"
}
```
*Possible `mode` values:* `TRAP` (Active AI Honeypot), `MONITOR` (Silent Passive), `STANDBY` (Disengaged).

---

### 3.3 Session Reset
Resets all chat buffers, telemetry counters, and generates a fresh case ID.

* **Endpoint:** `POST /api/reset`
* **Response (200 OK):**
```json
{
  "status": "RESET_SUCCESS",
  "case_id": "NCRP-CYBER-20261004-918234"
}
```

---

## 4. Multi-Modal Tool & Canary Endpoints

### 4.1 Dynamic Failed Receipt Generator
Renders a pixel-perfect PNG image of a PhonePe/GPay U16 failed payment screenshot.

* **Endpoint:** `GET /tools/fake-receipt`
* **Query Parameters:**
  * `upi` (string, default: `payee@upi`): Payee UPI handle.
  * `amount` (string, default: `500`): Numerical transaction amount.
* **Response:** `200 OK (image/png)`

---

### 4.2 Forensic Canary Tracking Page
The public landing page visited by the scammer when clicking the verification link.

* **Endpoint:** `GET /receipt/{receipt_id}`
* **Headers Captured:** `CF-Connecting-IP`, `X-Forwarded-For`, `User-Agent`.
* **Behavior:** Logs visitor network telemetry to `STATE["canary_hits"]` and presents an authentic "Bank Transaction Verification" hold page.

---

### 4.3 Section 63 BSA Forensic Evidence PDF
Compiles and streams the court-admissible 1930 NCRP electronic evidence PDF.

* **Endpoint:** `GET /download-fir`
* **Response:** `200 OK (application/pdf)` with header `Content-Disposition: attachment; filename=NCRP_1930_FIR_NCRP-CYBER-....pdf`.
