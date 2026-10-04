# 🏛️ Technical Architecture & System Design — ScamTrap AI

This document provides an in-depth architectural breakdown of ScamTrap AI, describing component relationships, data pipelines, thread concurrency models, multi-LLM routing, and network protocols.

---

## 1. High-Level Architecture Overview

ScamTrap AI is designed as a **hybrid multi-service architecture** composed of:
1. **The Physical WhatsApp Device Bridge** (`wa_bridge.js` + Node.js Puppeteer): Direct multi-device connection to WhatsApp Web.
2. **The Forensic Ingestion & Agentic Backend** (`app.py` + FastAPI / Uvicorn): High-speed API backend, regex intel extraction, LLM rotation chain, Pillow screenshot rendering, and PDF compilation.
3. **The Human Behavioral Simulation Engine** (`human_engine.py`): Algorithmic typing latency, hesitation pacing, and conversational bubble splitting.
4. **The Security Operations Center (SOC) Dashboard** (HTML5 + TailwindCSS + Leaflet GIS): Live forensic monitoring interface.

```
+---------------------------------------------------------------------------------------+
|                                    ScamTrap AI Core                                   |
+---------------------------------------------------------------------------------------+
|                                                                                       |
|   +---------------------+       HTTP Webhook       +-------------------------------+  |
|   |   WhatsApp Bridge   | ───────────────────────> |       FastAPI Backend         |  |
|   |   (wa_bridge.js)    | <─────────────────────── |           (app.py)            |  |
|   +----------+----------+       Outbox Poll        +---------------+---------------+  |
|              |                                                     |                  |
|              | Puppeteer / WWebJS                                  | ThreadPool       |
|              v                                                     v                  |
|   +---------------------+                          +-------------------------------+  |
|   | Real WhatsApp Phone |                          | Multi-LLM Provider Chain      |  |
|   |   (Phone 1 Linked)  |                          | (Groq, OpenRouter, Gemini)    |  |
|   +---------------------+                          +---------------+---------------+  |
|                                                                    |                  |
|                                                                    v                  |
|   +---------------------+       WebSocket/HTTP     +-------------------------------+  |
|   |  SOC Dashboard UI   | <─────────────────────── |   State & Intel Store (RAM)   |  |
|   | (Leaflet / Tailwind)|                          |  - Thread-Safe (STATE_LOCK)   |  |
|   +---------------------+                          +-------------------------------+  |
|                                                                                       |
+---------------------------------------------------------------------------------------+
```

---

## 2. Component Breakdown

### 2.1 WhatsApp Web Bridge (`wa_bridge.js`)
* **Role:** Intercepts real WhatsApp events and acts as the bidirectional communication bridge.
* **Technology:** `whatsapp-web.js` over headless Chromium (Puppeteer).
* **Session Persistence:** `LocalAuth` stores session tokens in `.wwebjs_auth/`, eliminating the need to re-scan QR codes across restarts.
* **Identity Resolution (`sender_identity.js`):** WhatsApp occasionally uses internal LIDs (e.g. `123456789@lid`) instead of international phone numbers (`919876543210@c.us`). The bridge inspects chat metadata, contact objects, and message headers to normalize all senders to standard E.164 phone numbers.

### 2.2 Forensic Ingestion & Agentic Backend (`app.py`)
* **Framework:** FastAPI running asynchronously on Uvicorn worker threads.
* **State Management:** Fully thread-safe global in-memory dictionary (`STATE`) guarded by a re-entrant lock (`STATE_LOCK`).
* **Regex Intelligence Extractor:**
  * **UPI IDs:** `\b[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}\b` (excluding common webmail domains).
  * **Indian Phone Numbers:** `(?:\+91[\-\s]?)?[6-9]\d{9}\b`
  * **IFSC Codes:** `\b[A-Z]{4}0[A-Z0-9]{6}\b`
  * **Bank Accounts:** `\b\d{11,18}\b`

---

## 3. Agentic Decision Loop & Multi-LLM Routing

```
                         [Incoming Suspect Message]
                                     │
                                     ▼
                    [Regex Intelligence Extractor]
                     - UPI, Bank A/C, IFSC, Phones
                                     │
                                     ▼
                    [Intent & Threat Classifier]
                     - Payment Demand / Task / Bill / Casual
                                     │
                                     ▼
                     [Multi-Provider LLM Chain]
                     ├── Priority 1: Groq (llama-3.3-70b / gpt-oss)
                     ├── Priority 2: OpenRouter (Gemma / Qwen / Free)
                     ├── Priority 3: Pollinations.ai (Keyless floor)
                     └── Priority 4: Google Gemini (gemini-2.5-flash)
                                     │
                                     ▼
                       [Anti-Hallucination Gate]
                     - Strips AI mentions & verifies tools
                                     │
                        ┌────────────┴────────────┐
                        ▼                         ▼
             [Tool: Fake UPI Glitch]    [Tool: Canary Trap]
             - Pillow PNG Renderer      - Cloudflare Token URL
                        │                         │
                        └────────────┬────────────┘
                                     │
                                     ▼
                     [Human Behavioral Delivery Plan]
                     - Dynamic Read Latency
                     - Thumb Typing Simulation
                     - Conversational Bubble Split
                                     │
                                     ▼
                        [Outbound Dispatch to Bridge]
```

### Quota Cooldown & Failover Algorithm
Every LLM provider and API key represents an isolated quota bucket with independent health tracking:
* On receiving `HTTP 429` (Rate Limited) or `RESOURCE_EXHAUSTED`, the system parses the `retry-after` header and sets `GEMINI_COOLDOWN[provider_key] = time.now() + cooldown_seconds`.
* The router immediately executes the next provider in the chain **without blocking or stalling the incoming thread**.
* If all external providers are cooling down, the system instantly fails over to the zero-dependency deterministic tactical engine.

---

## 4. Human Behavioral Engine (`human_engine.py`)

A fundamental flaw of basic AI chatbots is that they respond instantaneously with monolithic blocks of text. Human beings typing on WhatsApp exhibit clear physical patterns:

```
Step 1: Read & Comprehension Latency
  Duration: 3,000ms + (Message Length * 14ms)
  Action: Victim reads the incoming text and decides how to react.

Step 2: Typing State Onset
  Duration: 600ms - 1,400ms
  Action: Victim opens the input field (WhatsApp typing indicator turns ON).

Step 3: Thumb Typing Cadence
  Speed: 150ms - 330ms per character
  Hesitation: 40ms - 180ms extra between words.
  Mid-message Pauses: 22% probability of a 2-6s distraction pause.

Step 4: Conversational Bubble Splitting
  Rule: Long replies are split across punctuation boundaries into 1-3 short bubbles.
  Gap: 900ms - 3,800ms natural hesitation between sending bubble 1 and bubble 2.
```

---

## 5. Multi-Modal Tool Architecture

### 5.1 Dynamic NPCI U16 Failed Payment Screenshot Generator (Pillow)
* **Canvas Resolution:** 720 x 1340 pixels (Android 1080p viewport scaling).
* **Dynamic Graphic Elements:**
  * Vector-drawn Android status bar: time, battery percentage pill (81-88%), Wi-Fi concentric arcs, cellular 4-bar cluster, and 5G VoLTE badge.
  * PhonePe purple navigation bar (`#5F259F`) with back navigation arrow and help circle.
  * Center Hero Card: Vector red cross circle (`#E53935`), dynamic formatted amount (e.g., `₹1,500.00`), scammer payee handle, and exact timestamp.
  * Official NPCI Error Card: *"Payment Failed - Daily limit exceeded for this bank account (U16)"*.

### 5.2 Forensic Canary IP & Geolocation Trap
* **Token Structure:** `/receipt/TXN-{UUID6}`
* **Public Tunneling:** Auto-spawns a free Cloudflare Quick Tunnel (`cloudflared.exe`) binding `http://127.0.0.1:8000` to a public `https://*.trycloudflare.com` URL.
* **Forensic Data Captured on Visitor Click:**
  * Client IP Address (`CF-Connecting-IP` / `X-Forwarded-For`)
  * ISP / Autonomous System Number (ASN)
  * Browser User-Agent & OS Fingerprint
  * IP-based Geolocation (Latitude, Longitude, City, Region, Country)
  * Optional High-Precision Geolocation (HTML5 Geolocation API with visitor consent).

---

## 6. Security, Authentication & Threat Model

1. **Loopback Trust Boundary:** Requests originating directly from `127.0.0.1` / `localhost` (such as the local Node.js bridge and local browser) are trusted automatically.
2. **Tunnel API Protection:** Any request arriving via reverse proxies, Cloudflare tunnels, or remote hosts must provide a matching `SCAMTRAP_API_KEY` header (`X-API-Key` or `Authorization: Bearer <key>`) validated via constant-time comparison (`hmac.compare_digest`).
3. **HTTP Security Headers:**
   * `X-Content-Type-Options: nosniff`
   * `X-Frame-Options: SAMEORIGIN`
   * `X-XSS-Protection: 1; mode=block`
   * `Referrer-Policy: strict-origin-when-cross-origin`
   * `Cache-Control: no-store` (prevents telemetry caching).
