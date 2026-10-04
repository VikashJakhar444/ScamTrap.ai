# 🛡️ ScamTrap AI — Autonomous WhatsApp Scammer Honeypot & Counter-Deception Engine

[![Hackathon](https://img.shields.io/badge/HackPulse%202026-Agentic%20Harness%20Track-9F1239?style=for-the-badge)](https://github.com/VikashJakhar444/ScamTrap.ai)
[![FastAPI](https://img.shields.io/badge/Backend-FastAPI-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com)
[![WhatsApp Web](https://img.shields.io/badge/Bridge-WhatsApp--Web.js-25D366?style=for-the-badge&logo=whatsapp&logoColor=white)](https://wwebjs.dev/)
[![License](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)](LICENSE)

> **"Turning the Hunter into the Hunted"**  
> ScamTrap AI is an autonomous, multi-modal cyber counter-deception platform that engages WhatsApp scammers, simulates believable victim personas with human typing cadence, deploys dynamic fake UPI glitch screenshots and forensic tracking links, extracts mule banking intelligence, and generates court-admissible Section 63 BSA-certified police FIR reports in real time.

---

## 📌 Table of Contents
- [The Problem](#-the-problem)
- [Why ScamTrap AI?](#-why-scamtrap-ai)
- [Key Innovations](#-key-innovations)
- [System Architecture](#-system-architecture)
- [SOC Command Dashboard](#-soc-command-dashboard)
- [Quickstart Guide](#-quickstart-guide)
- [Repository Structure](#-repository-structure)
- [Bot Self-Chat Commands](#-bot-self-chat-commands)
- [Legal & Forensic Compliance](#-legal--forensic-compliance)
- [Pitch Deck & Presentation](#-pitch-deck--presentation)

---

## 🚨 The Problem

In India, **₹1,750+ Crores** are lost annually to cyber scams *(electricity bill disconnection, task/job scams, digital arrest, and fake customs parcel extortion)*.

* **The Scammer Advantage:** Scammers broadcast thousands of WhatsApp messages. Even a 0.5% conversion nets them lakhs using temporary mule bank accounts.
* **The Flawed Status Quo:** Victims can only block or report numbers. This inflicts **zero cost** on scammers, who instantly move to their next target without losing their mule banking infrastructure.

---

## 💡 Why ScamTrap AI?

Instead of blocking scammers, **ScamTrap AI mounts an active autonomous counter-offensive**:

| Feature | Standard Anti-Spam (Truecaller / WhatsApp Report) | ScamTrap AI Autonomous Honeypot |
| :--- | :--- | :--- |
| **Strategy** | Passive number blocking | **Active autonomous deception & engagement** |
| **Mule Account Discovery** | ❌ None (number discarded) | ✅ **100% Real-time UPI, Bank A/C & IFSC Extraction** |
| **Geographic Attribution** | ❌ None | ✅ **Canary IP, ISP & Device Geolocation Pin** |
| **Scammer Economic Cost** | ❌ Zero seconds wasted | ✅ **Maximum time, attention & compute drain** |
| **Law Enforcement Output** | ❌ Unverified user screenshots | ✅ **Section 63 BSA-certified 1930 Forensic PDF** |

---

## ⚡ Key Innovations

### 1. 🧠 Autonomous Agentic Decision Loop
* Evaluates conversation context with multi-provider rotation across **Groq, OpenRouter, Pollinations, and Google Gemini**.
* Automatically selects contextual payloads (`NONE`, `SEND_FAKE_UPI_GLITCH`, `SEND_CANARY_LINK`) without rigid scripted constraints.
* Features a high-speed deterministic rule engine fallback for zero-downtime execution.

### 2. ⏳ Turing-Grade Human Behavioral Engine (`human_engine.py`)
Bots get fingerprinted instantly when they reply in 1 second. ScamTrap simulates true human WhatsApp behavior:
* **Proportional Read Latency:** Simulates human reading time (3–8s) scaling with incoming message length.
* **Realistic Thumb Typing:** Simulates thumb typing at 150–330ms per character with pauses between words.
* **Conversational Bubble Splitting:** Automatically splits responses across sentence boundaries into 1–3 realistic message bubbles.
* **Adversarial Probe Deflection:** Casually handles math challenges (*"what is 15+7"*), memory probes, and "are you a bot" suspicion without breaking persona.

### 3. 🎨 Multi-Modal Dynamic Tooling
* **Pixel-Perfect PhonePe/GPay Failed Receipt Generator (Pillow):** Dynamically generates authentic Android status bars, vector failure icons, and official NPCI U16 limit error cards stamped with the **scammer's actual UPI ID & exact demanded amount**. This forces the scammer to reveal a **backup mule account**.
* **Canary IP & Geolocation Trap:** Injects a contextual "Bank IMPS Clearance Verification" link that captures the scammer's **Public IP, ISP/ASN, Browser Fingerprint, and Approximate Location Coordinates**, plotted live on the SOC Leaflet map.

### 4. 🧭 Attack Kill-Chain & Intelligence Extraction
Visualizes the suspect's progression across 5 tactical stages:
1. `APPROACH`: Initial incoming probe.
2. `RAPPORT`: Establishing naive victim trust.
3. `PRESSURE`: Detection of urgency, deadlines, legal threats, or task fees.
4. `PAYMENT DEMAND`: Harvesting suspect payment vectors.
5. `NEUTRALIZATION`: Fake glitch / canary delivery, stalling fraud and preventing financial loss.

### 5. ⚖️ Section 63 BSA-Compliant Evidence & 1930 NCRP PDF
* Exports court-admissible electronic evidence strictly formatted under **Section 63 of the Bharatiya Sakshya Adhiniyam (BSA), 2023**.
* Includes chronological transcripts, extracted mule banking tables, Canary network audit trails, and embedded cryptographic hash stamps ready for the **National Cyber Crime Helpline (1930)**.

---

## 🏗️ System Architecture

```
                      ┌──────────────────────────────┐
                      │    Scammer (Phone 2)         │
                      └──────────────┬───────────────┘
                                     │ WhatsApp Message
                                     ▼
                      ┌──────────────────────────────┐
                      │ WhatsApp Web Bridge (Node.js)│
                      │ Puppeteer / LocalAuth Client │
                      └──────────────┬───────────────┘
                                     │ Secure API Webhook
                                     ▼
                      ┌──────────────────────────────┐
                      │ FastAPI Backend & Intel Engine│
                      │ - Regex IOC Extractor (UPI)  │
                      │ - Threat & Intent Classifier │
                      └──────────────┬───────────────┘
                                     │ Multi-LLM Fallback Pipeline
                                     ▼
                      ┌──────────────────────────────┐
                      │ Agentic Decision Engine      │
                      │ (Groq ➔ OpenRouter ➔ Gemini) │
                      │ Decision: Text + Tool Payload│
                      └──────────────┬───────────────┘
                                     │ Timed Delivery Plan
                                     ▼
                      ┌──────────────────────────────┐
                      │ Human Behavior Engine        │
                      │ - Variable Read Latency      │
                      │ - Human Thumb Typing Speed   │
                      │ - Conversational Bubble Split│
                      └──────────────┬───────────────┘
                                     │ Timed Execution
                                     ▼
                      ┌──────────────────────────────┐
                      │ Scammer Receives Reply /     │
                      │ PIL Receipt / Canary Link    │
                      └──────────────────────────────┘
```

---

## 🖥️ SOC Command Dashboard

The built-in Security Operations Center (SOC) dashboard (`http://localhost:8000`) provides live situational awareness:
* **Live WhatsApp Stream:** Real-time dual-role chat feed showing AI thinking and typing indicators.
* **Live Canary Radar Map:** Interactive Leaflet GIS map pinning the suspect's IP, ISP, device, and estimated location coordinates.
* **Mule Intel Vault:** Real-time captured UPI handles, bank accounts, IFSC codes, and mobile numbers.
* **Scammer Dossier & Aggression Gauge:** Dynamic risk score, threat playbook classification, and language profiling.
* **1-Click 1930 FIR PDF Generation:** Instant export of court-ready electronic evidence dossiers.

---

## 🚀 Quickstart Guide

### Prerequisites
- **Python 3.10+**
- **Node.js 18+** & **npm**
- **Google Chrome** (or Chromium for Puppeteer)

### 1. Clone the Repository
```bash
git clone https://github.com/VikashJakhar444/ScamTrap.ai.git
cd ScamTrap.ai
```

### 2. Environment Configuration
Create a `.env` file in the project root:
```env
# Google Gemini API Key (Optional)
GEMINI_API_KEY=""

# Groq API Key (Recommended for fast inference)
GROQ_API_KEY="your_groq_api_key"

# Public Canary Tunnel (Optional - auto-discovered via cloudflared if present)
PUBLIC_TUNNEL_URL=""

# Server Configuration
HOST=127.0.0.1
PORT=8000
```

### 3. 1-Click Launch
Run the automated cross-platform service manager:
```bash
python run.py
```
* The launcher will set up Python virtual environment (`.venv`), install dependencies, check Node.js modules, free occupied ports, and start both the **FastAPI Backend** and the **WhatsApp Web Bridge**.
* Scan the terminal QR code using **WhatsApp → Linked Devices → Link a Device** on **Phone 1 (Honeypot Phone)**.
* Open **`http://localhost:8000`** in your browser to monitor the live SOC dashboard.

---

## 📁 Repository Structure

```
ScamTrap/
├── app.py                   # FastAPI backend, agentic routing, PIL generator & SOC dashboard
├── human_engine.py          # Human behavioral simulation (delays, thumb typing, bubble splitting)
├── run.py                   # 1-Click automated cross-platform service launcher
├── wa_bridge.js             # WhatsApp Web bridge (whatsapp-web.js / Puppeteer)
├── sender_identity.js       # WhatsApp LID / JID identity normalization helper
├── HACKPULSE_PITCH_DECK.md  # 12-Slide Hackathon Presentation Deck & Live Demo Script
├── requirements.txt         # Python dependencies (FastAPI, Uvicorn, Pillow, ReportLab, etc.)
├── package.json             # Node.js dependencies (whatsapp-web.js, qrcode-terminal, axios)
└── tests/                   # Automated test suite (conversation, geo, sender identity)
```

---

## 💬 Bot Self-Chat Commands

You can control ScamTrap AI directly from WhatsApp by messaging yourself on **Phone 1**:

| Command | Action |
| :--- | :--- |
| `trap <phone>` | Manually engage **Active Trap (AI Hijack)** mode on a specific number. |
| `monitor <phone>` | Set number to **Passive Monitor** mode (logs silently, alerts on payment demands). |
| `standby <phone>` | Release number to **Standby** mode. |
| `status` | Query active mode, tracked suspects, and extracted IOC counts. |
| `help` | Display command help sheet in WhatsApp chat. |

---

## ⚖️ Legal & Forensic Compliance

ScamTrap AI operates exclusively on **consensual honeypot phone numbers** for cyber defense and research. All extracted evidence dossiers are generated in strict adherence to:
* **Section 63 of the Bharatiya Sakshya Adhiniyam (BSA), 2023** *(Conditions of admissibility of electronic records)*.
* **National Cyber Crime Reporting Portal (NCRP / 1930 Helpline)** standard operating procedures.

---

## 🏆 Pitch Deck & Presentation

For the complete hackathon presentation slides, word-for-word 15-second hook, and judge Q&A defense strategy, see:  
👉 **[HACKPULSE_PITCH_DECK.md](HACKPULSE_PITCH_DECK.md)**

---

## 📄 License
This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.
