# 🛠️ Technology Stack & Dependencies — ScamTrap AI

This document details the complete software stack, third-party libraries, protocols, and architectural justifications utilized across ScamTrap AI.

---

## 1. Stack Overview Matrix

| Domain | Technology / Library | Version | Role in ScamTrap AI |
| :--- | :--- | :--- | :--- |
| **Backend Core** | **Python** | `3.10+` | Primary backend language and forensic logic engine |
| **Web Framework** | **FastAPI** | `^0.115.0` | High-performance asynchronous REST API server |
| **ASGI Server** | **Uvicorn** | `^0.32.0` | Asynchronous worker process manager |
| **Data Validation** | **Pydantic** | `^2.10.0` | Strict JSON schema parsing and agent output validation |
| **WhatsApp Bridge** | **Node.js** | `18.x / 20.x` | Multi-device WhatsApp Web automation runtime |
| **WA Protocol** | **whatsapp-web.js** | `^1.26.0` | Headless WhatsApp Web client protocol |
| **Browser Engine** | **Puppeteer / Chromium** | `^23.0.0` | Headless browser execution for WhatsApp Web session |
| **LLM Provider 1** | **Groq API** | Cloud | Ultra-low latency inference (Llama 3.3 70B / GPT-OSS) |
| **LLM Provider 2** | **OpenRouter API** | Cloud | Diversified open-weights models (Gemma / Qwen) |
| **LLM Provider 3** | **Google GenAI SDK** | `^0.1.0` | Gemini 2.5 Flash native agentic inference |
| **LLM Provider 4** | **Pollinations.ai** | Cloud | Keyless fallback inference floor |
| **Image Generation**| **Pillow (PIL)** | `^11.0.0` | Dynamic pixel-perfect PhonePe/GPay receipt rendering |
| **PDF Generation**  | **ReportLab** | `^4.2.5` | Section 63 BSA-compliant legal FIR PDF generation |
| **GIS Mapping**     | **Leaflet.js** | `1.9.4` | Interactive SOC forensic radar map |
| **Styling & UI**    | **TailwindCSS** | `3.x (CDN)` | Security Operations Center (SOC) dark-mode design |
| **Tunneling**       | **Cloudflare Quick Tunnels** | `2024.x` | Automatic public HTTPS reverse proxy for canary links |

---

## 2. Component Justification & Selection Rationale

### 2.1 Backend: FastAPI & Python
* **Why FastAPI?**
  * Asynchronous routing with native concurrency support ensures high-throughput webhook handling.
  * Automatic OpenAPI documentation and Pydantic schema validation prevent malformed payload injections.
  * Native threadpool offloading allows CPU-heavy tasks (PIL image rendering and ReportLab PDF compilation) to run without blocking the event loop.

### 2.2 WhatsApp Bridge: whatsapp-web.js & Puppeteer
* **Why whatsapp-web.js?**
  * Official WhatsApp Cloud API requires pre-registered business templates, making dynamic adversarial deception and victim honeypot simulation impossible.
  * `whatsapp-web.js` emulates a real WhatsApp Web multi-device client, allowing full freedom to send and receive arbitrary text, media, and typing status events.
  * `LocalAuth` persists browser session cookies and tokens in `.wwebjs_auth/`, ensuring zero session drops across server restarts.

### 2.3 Multi-LLM Routing Pipeline
* **Why Multi-Provider Rotation?**
  * Free-tier AI APIs impose strict Request-Per-Minute (RPM) and Daily Request caps (HTTP 429).
  * ScamTrap implements an **adaptive quota-aware failover chain**:
    1. **Groq:** Primary provider offering ~400 tokens/second inference for real-time natural replies.
    2. **OpenRouter:** Secondary provider offering diverse open-weights models.
    3. **Google Gemini (2.5 Flash):** Deep reasoning agent with native structured JSON output (`AgentDecision`).
    4. **Pollinations.ai:** Keyless anonymous emergency floor that never runs out of quota.
    5. **Deterministic Tactical Engine:** Hardened rule-based regex fallback requiring zero network dependencies.

### 2.4 Graphics & Document Engines: Pillow & ReportLab
* **Why Pillow (PIL)?**
  * Pre-rendered static template images look fake when amounts or UPI IDs change.
  * Pillow dynamically draws custom vector geometry, fonts, system icons (5G, battery, Wi-Fi), and timestamps down to the exact pixel, producing screenshots that pass human visual inspection.
* **Why ReportLab?**
  * Generates high-resolution, vector-drawn PDF evidence dossiers.
  * Supports programmatic table formatting, custom styles, dynamic page counts, and embedded Section 63 BSA compliance certificates.

### 2.5 SOC Frontend: Leaflet.js & TailwindCSS
* **Why Leaflet.js?**
  * Lightweight open-source GIS mapping library requiring zero proprietary API keys (unlike Google Maps).
  * Uses ESRI World Imagery and Dark Tile layers with custom pulsating radar markers for live suspect attribution.

---

## 3. Dependency Manifests

### Python Dependencies (`requirements.txt`)
```txt
fastapi>=0.115.0
uvicorn>=0.32.0
pydantic>=2.10.0
google-genai>=0.1.0
requests>=2.32.0
pillow>=11.0.0
reportlab>=4.2.5
```

### Node.js Dependencies (`package.json`)
```json
{
  "name": "scamtrap-ai-bridge",
  "version": "2.0.0",
  "description": "WhatsApp Web automation bridge for ScamTrap AI",
  "main": "wa_bridge.js",
  "scripts": {
    "start": "node wa_bridge.js"
  },
  "dependencies": {
    "axios": "^1.7.9",
    "qrcode-terminal": "^0.12.0",
    "whatsapp-web.js": "^1.26.0"
  }
}
```
