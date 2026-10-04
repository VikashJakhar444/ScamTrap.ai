# 📋 Product Requirements Document (PRD) — ScamTrap AI

**Product Name:** ScamTrap AI  
**Version:** 2.0 (HackPulse 2026 Production Release)  
**Status:** Live / Active  
**Author:** ScamTrap AI Engineering Team  
**Track:** Agentic Harness & Open Innovation  

---

## 1. Executive Summary & Vision
ScamTrap AI is an autonomous, multi-modal cyber counter-deception platform designed to address the escalating cyber fraud crisis in India. Instead of passive defense mechanisms (such as blocking or reporting numbers), ScamTrap AI deploys an undercover AI honeypot that actively engages scammers on WhatsApp, simulates believable human conversational behavior, deploys fake payment failure screenshots and forensic tracking links, extracts mule banking intelligence, and compiles Section 63 BSA-certified evidence dossiers for law enforcement (1930 Helpline / NCRP).

---

## 2. Problem Statement & Market Opportunity
* **The Asymmetry of Cyber Fraud:** Cyber syndicates broadcast automated scam templates (electricity bills, part-time tasks, digital arrest threats) to millions. Even a conversion rate of 0.1% yields massive illicit profits.
* **Flaws of Status Quo Anti-Spam Tools:**
  * **Truecaller / WhatsApp Reporting:** Only blacklists the phone number. The scammer loses zero capital, discards the temporary SIM, and moves to the next victim without losing their underlying mule bank account network.
  * **Victim Trauma & Financial Loss:** Citizens have no automated mechanism to stall scammers, verify threats, or capture forensic evidence.
* **Opportunity:** An autonomous honeypot that converts every scam attempt into an operational and economic trap for the criminal, wasting their time while harvesting actionable Indicators of Compromise (IOCs).

---

## 3. User Personas & Target Audience
1. **Primary Victim / End User (The Honeypot Host):**
   * Everyday smartphone users targeted by scam messages on WhatsApp.
   * Benefit: Seamless autonomous protection; potential threats are deflected without user stress.
2. **Cyber Defense Analysts & SOC Teams:**
   * Enterprise IT security teams and cyber intelligence cells monitoring scam operations targeting company executives or customer bases.
   * Benefit: Live telemetry, threat playbook identification, and aggression analytics.
3. **Law Enforcement Agencies (LEAs / Cyber Crime Police / 1930 Operators):**
   * Police officers investigating cyber fraud networks.
   * Benefit: Instant, Section 63 BSA-certified electronic FIR dossiers with verified mule account details and IP forensic trails.

---

## 4. Product Goals & Core Objectives
* **Goal 1 (Autonomous Engagement):** 100% automated threat triage and conversational engagement on real WhatsApp devices without requiring manual supervision.
* **Goal 2 (Anti-Fingerprinting):** Indistinguishable human behavior simulation (variable read latency, thumb typing cadence, multi-bubble message splitting).
* **Goal 3 (Intelligence Yield):** Maximum extraction rate of financial mule handles (UPI IDs, Bank A/C, IFSC) and network attribution (IP, ISP, device fingerprint).
* **Goal 4 (Zero Downtime / Robustness):** High-availability multi-LLM rotation fallback pipeline ensuring uninterrupted execution under API rate limits (HTTP 429).
* **Goal 5 (Legal Admissibility):** Strict compliance with the Bharatiya Sakshya Adhiniyam (BSA), 2023 for court-ready evidence export.

---

## 5. Functional Requirements

### 5.1 Real 2-Phone WhatsApp Bridge
* **FR-1.1:** System shall connect to Phone 1 (Victim Phone) via WhatsApp Web QR code authentication using `whatsapp-web.js` with `LocalAuth` multi-device session persistence.
* **FR-1.2:** System shall forward all incoming suspect WhatsApp messages from Phone 2 (Scammer) to the FastAPI backend webhook within < 500ms.
* **FR-1.3:** System shall normalize sender identities, resolving temporary LIDs (Linked Identity IDs) to standard phone numbers.

### 5.2 Autonomous Threat Triage & Mode Routing
* **FR-2.1 (Auto-Trap Mode):** When an incoming message contains scam triggers (electricity bills, KYC blocks, task fees, threats) or IOCs (UPI/bank details), the engine shall automatically engage `TRAP` mode.
* **FR-2.2 (Smart Monitor Mode):** For ambiguous numbers, system shall passively log messages, auto-generating stall replies only when money is explicitly demanded.
* **FR-2.3 (WhatsApp Self-Chat Control):** User can command the honeypot by typing `trap <phone>`, `monitor <phone>`, `standby <phone>`, or `status` in their own "Message Yourself" WhatsApp chat.

### 5.3 Agentic Multi-LLM Decision Engine
* **FR-3.1:** Engine shall prioritize LLM providers according to `MODEL_PRIORITY` (Groq ➔ OpenRouter ➔ Pollinations ➔ Google Gemini).
* **FR-3.2:** On HTTP 429 / quota exhaustion, engine shall automatically apply exponential backoff cooldowns and failover to the next active provider in < 1 second.
* **FR-3.3:** If all external providers are unavailable, engine shall seamlessly drop to the deterministic tactical rule engine (`get_deterministic_tactical_reply`).
* **FR-3.4 (Anti-Hallucination Guard):** Model output shall pass through `_sanitize_model_reply` to intercept and reject any self-revealing phrases ("I am an AI", "honeypot", "language model").

### 5.4 Multi-Modal Tool Execution
* **FR-4.1 (PhonePe/GPay Failed Receipt Generator):**
  * System shall generate pixel-perfect PNG screenshots using Pillow.
  * Screenshots must dynamically display the scammer's extracted UPI ID, exact demanded amount, authentic Android status bar (battery, 5G VoLTE, time), and official NPCI U16 Daily Limit Exceeded error cards.
* **FR-4.2 (Canary Forensic IP Trap):**
  * System shall generate unique transaction verification URLs (`/receipt/TXN-XXXX`).
  * When opened, the endpoint shall record the visitor's Public IP, ISP/ASN, User-Agent, Browser Fingerprint, and optional Geolocation coordinates.

### 5.5 Forensic SOC Command Dashboard
* **FR-5.1:** Real-time dual-role live conversation stream with active typing indicators and AI live thought ticker.
* **FR-5.2:** Interactive Leaflet GIS Radar Map displaying Canary hit pins with detailed ISP and device popup cards.
* **FR-5.3:** Mule Intelligence Vault categorizing UPI handles, phone numbers, bank accounts, and IFSC codes.
* **FR-5.4:** Attack Kill-Chain Funnel displaying real-time suspect progression across `APPROACH`, `RAPPORT`, `PRESSURE`, `PAYMENT_ASK`, and `NEUTRALIZATION`.

### 5.6 Legal PDF Evidence Generation
* **FR-6.1:** 1-Click download of official NCRP 1930 FIR PDF dossiers generated via ReportLab.
* **FR-6.2:** Dossier must include incident case IDs (`NCRP-CYBER-YYYYMMDD-XXXXXX`), complete timestamped transcripts, extracted IOC tables, Canary network metadata, and signed Section 63 BSA electronic certificates.

---

## 6. Non-Functional Requirements (NFRs)
* **NFR-1 (Performance & Latency):** Ingestion webhook latency must be < 50ms. Human typing simulation must naturally span 4s to 25s depending on message length.
* **NFR-2 (Security & Authentication):** All `/api/*` endpoints reached via external proxies/tunnels must require a valid `X-API-Key` or Bearer Token (`SCAMTRAP_API_KEY`). Direct loopback (`127.0.0.1`) connections are automatically trusted.
* **NFR-3 (Memory Safety & Concurrency):** In-memory state (`STATE`) must be protected by re-entrant thread locks (`STATE_LOCK`). Chat logs and thought ticker buffers must have FIFO eviction caps (max 500 items).
* **NFR-4 (Cross-Platform Compatibility):** System must run natively on Windows 10/11, macOS, and Linux (Ubuntu 20.04+).

---

## 7. Success Metrics & Key Performance Indicators (KPIs)
| Metric | Target Value |
| :--- | :--- |
| **Autonomous Threat Triage Accuracy** | > 96% |
| **Mule Account (UPI/Bank) Capture Rate** | > 90% on payment demands |
| **Average Scammer Engagement Duration** | > 8 conversation turns |
| **Anti-Fingerprinting Pass Rate** | Zero bot accusations or premature exits |
| **LLM Provider Failover Latency** | < 800ms on HTTP 429 |
| **FIR PDF Generation Time** | < 1.5 seconds |
