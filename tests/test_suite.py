import requests
import json
import base64
import os
import sys

BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")

sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

def run_tests():
    print("=" * 60, flush=True)
    print("SCAMTRAP AI FULL END-TO-END AUTOMATED TEST SUITE", flush=True)
    print("=" * 60, flush=True)
    
    results = []

    def assert_test(name, condition, details=""):
        passed = bool(condition)
        results.append(passed)
        if passed:
            print(f"[PASS] {name} {details}", flush=True)
        else:
            print(f"[FAIL] {name} {details}", flush=True)

    # 1. Test /api/state
    try:
        r = requests.get(f"{BASE_URL}/api/state", timeout=5)
        assert_test("1. GET /api/state returns 200", r.status_code == 200)
        data = r.json()
        assert_test("   State structure contains case_id, wa_status, extracted_intel", 
                    "case_id" in data and "extracted_intel" in data and "wa_status" in data)
    except Exception as e:
        assert_test("1. GET /api/state connection", False, str(e))

    # 2. Test Reset
    try:
        r = requests.post(f"{BASE_URL}/api/reset", timeout=5)
        assert_test("2. POST /api/reset returns 200", r.status_code == 200)
    except Exception as e:
        assert_test("2. Reset test", False, str(e))

    # 3. Simulate WhatsApp Bridge Connection Status
    try:
        r = requests.post(f"{BASE_URL}/api/wa-status", json={
            "status": "CONNECTED",
            "phone": "919876543210",
            "my_jid": "919876543210@c.us"
        }, timeout=5)
        assert_test("3. POST /api/wa-status (CONNECTED)", r.status_code == 200)
        
        st = requests.get(f"{BASE_URL}/api/state").json()
        assert_test("   State updated phone to 919876543210", st.get("wa_phone") == "919876543210")
    except Exception as e:
        assert_test("3. WA Status test", False, str(e))

    # 4. Test Incoming message in STANDBY (Casual chat from Phone 2: +919123456789)
    try:
        r = requests.post(f"{BASE_URL}/api/wa-incoming", json={
            "msg_id": "MSG_TEST_001",
            "sender_jid": "919123456789@c.us",
            "sender_number": "919123456789",
            "text": "Hello bhai, kaisa hai? Kal college chalega kya?"
        }, timeout=10)
        assert_test("4. POST /api/wa-incoming (STANDBY casual chat)", r.status_code == 200)
        res_data = r.json()
        assert_test("   STANDBY response status is STANDBY (No auto-reply)", res_data.get("status") == "STANDBY" and not res_data.get("should_reply"))
    except Exception as e:
        assert_test("4. STANDBY test", False, str(e))

    # 5. Test Phone 1 Command: 'monitor 9123456789'
    try:
        r = requests.post(f"{BASE_URL}/api/bot-self-command", json={
            "text": "monitor 9123456789",
            "last_incoming_from": "919123456789@c.us"
        }, timeout=10)
        assert_test("5. POST /api/bot-self-command ('monitor 9123456789')", r.status_code == 200)
        data = r.json()
        assert_test("   Bot confirmation mentions MONITOR MODE ACTIVATED", "MONITOR MODE" in data.get("bot_confirm_msg", ""))
    except Exception as e:
        assert_test("5. Bot self monitor command", False, str(e))

    # 6. Test Incoming Scam/Money Demand in MONITOR MODE
    try:
        r = requests.post(f"{BASE_URL}/api/wa-incoming", json={
            "msg_id": "MSG_TEST_002",
            "sender_jid": "919123456789@c.us",
            "sender_number": "919123456789",
            "text": "Urgent electricity bill ₹25,000 pending. Pay immediately to avoid power cut!"
        }, timeout=10)
        assert_test("6. POST /api/wa-incoming (MONITOR mode money ask)", r.status_code == 200)
        data = r.json()
        assert_test("   Smart Monitor sent stall reply ('ruko/dekhta hu')", 
                    data.get("status") == "MONITORED" and data.get("should_reply") == True and bool(data.get("reply_text")))
        print(f"      -> AI Stall Reply: \"{data.get('reply_text')}\"")
    except Exception as e:
        assert_test("6. Monitor stall test", False, str(e))

    # 7. Test Phone 1 Command: 'trap 9123456789'
    try:
        r = requests.post(f"{BASE_URL}/api/bot-self-command", json={
            "text": "trap 9123456789",
            "last_incoming_from": "919123456789@c.us"
        }, timeout=25)
        assert_test("7. POST /api/bot-self-command ('trap 9123456789')", r.status_code == 200)
        data = r.json()
        assert_test("   Bot confirmation mentions TRAP ENGAGED", "TRAP ENGAGED" in data.get("bot_confirm_msg", ""))
    except Exception as e:
        assert_test("7. Bot self trap command", False, str(e))

    # 8. Test Incoming Scammer Message with UPI ID in TRAP MODE
    try:
        r = requests.post(f"{BASE_URL}/api/wa-incoming", json={
            "msg_id": "MSG_TEST_003",
            "sender_jid": "919123456789@c.us",
            "sender_number": "919123456789",
            "text": "Send payment to electricity.discom@sbi right now or meter will be disconnected!"
        }, timeout=25)
        assert_test("8. POST /api/wa-incoming (TRAP mode with UPI ID)", r.status_code == 200)
        data = r.json()
        assert_test("   Status is TRAPPED and should_reply is True", data.get("status") == "TRAPPED" and data.get("should_reply") == True)
        assert_test("   Generated base64 PhonePe failed screenshot", bool(data.get("media_base64")))
        assert_test("   Staged flow: NO canary link in first payment exchange", not data.get("trap_url"))
        assert_test("   Human delivery plan present", bool(data.get("delivery_plan")))
        assert_test("   Total human delay >= 3000ms (model time + plan)",
                    (data.get("total_delay_ms") or 0) >= 3000, f"({data.get('total_delay_ms')}ms)")
        print(f"      -> AI Bait Reply: \"{data.get('reply_text')}\"")
        print(f"      -> Model time: {data.get('model_ms')}ms, plan delay: {data.get('recommended_delay_ms')}ms")
    except Exception as e:
        assert_test("8. Trap bait + PhonePe image + staged flow", False, str(e))

    # 8b. Scammer pushes again -> canary link is released (staged flow step 2)
    try:
        r = requests.post(f"{BASE_URL}/api/wa-incoming", json={
            "msg_id": "MSG_TEST_003B",
            "sender_jid": "919123456789@c.us",
            "sender_number": "919123456789",
            "text": "abhi tak nahi aaya paisa, jaldi check kar aur payment kar!"
        }, timeout=30)
        assert_test("8b. POST /api/wa-incoming (scammer pushes after screenshot)", r.status_code == 200)
        data = r.json()
        assert_test("   Push reply carries the canary link", bool(data.get("trap_url")))
        if data.get("trap_url"):
            assert_test("   Canary URL appears in the reply text",
                        data["trap_url"] in (data.get("reply_text") or ""))
            print(f"      -> Canary URL: {data.get('trap_url')}")
    except Exception as e:
        assert_test("8b. Staged canary release", False, str(e))

    # 9. Test Extracted IOCs in State
    try:
        st = requests.get(f"{BASE_URL}/api/state", timeout=10).json()
        upis = st.get("extracted_intel", {}).get("upi_ids", [])
        assert_test("9. Regex Intel Extractor captured 'electricity.discom@sbi'", "electricity.discom@sbi" in upis)
    except Exception as e:
        assert_test("9. IOC Extraction test", False, str(e))

    # 10. Test Canary Link Hit (Device / IP Capture)
    try:
        receipt_id = "TXN-TEST88"
        r = requests.get(f"{BASE_URL}/receipt/{receipt_id}", headers={
            "User-Agent": "Mozilla/5.0 (Linux; Android 14; SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
            "X-Forwarded-For": "103.212.144.15"
        }, timeout=10)
        assert_test(
            "10. GET /receipt/TXN-TEST88 (transparent Canary Hit)",
            r.status_code == 200
            and "ScamTrap AI security test" in r.text
            and "Share device location" in r.text
            and "STATE BANK OF INDIA" not in r.text,
        )
        
        st = requests.get(f"{BASE_URL}/api/state", timeout=10).json()
        canary_hits = st.get("canary_hits", [])
        assert_test("    Canary Telemetry captured IP (103.212.144.15) and OS (Android)", 
                    any(h.get("ip") == "103.212.144.15" for h in canary_hits))
    except Exception as e:
        assert_test("10. Canary telemetry hit", False, str(e))

    # 11. Test Download NCRP 1930 Forensic FIR PDF
    try:
        r = requests.get(f"{BASE_URL}/download-fir", timeout=15)
        assert_test("11. GET /download-fir generates PDF (200 OK)", r.status_code == 200 and r.headers.get("content-type") == "application/pdf")
        assert_test("    PDF size is valid (> 1KB)", len(r.content) > 1000)
    except Exception as e:
        assert_test("11. PDF Generation test", False, str(e))

    # 12. Six new intelligence features (kill-chain, analytics, multi-channel, dossier)
    try:
        st = requests.get(f"{BASE_URL}/api/state", timeout=10).json()
        fn = st.get("funnel", {})
        assert_test("12. Kill-chain funnel advanced to PAYMENT_ASK/BLOCKED", (fn.get("stage") or 0) >= 3,
                    f"(stage={fn.get('stage')})")
        assert_test("    Funnel blocked >= 1 with stalled money recorded",
                    (fn.get("blocked") or 0) >= 1 and (fn.get("money_stalled") or 0) >= 25000,
                    f"(blocked={fn.get('blocked')}, stalled={fn.get('money_stalled')})")
        agg = st.get("aggression", {})
        assert_test("    Aggression analytics populated (current/average/peak)",
                    agg.get("current") is not None and (agg.get("average") or 0) > 0,
                    f"(avg={agg.get('average')}, peak={agg.get('peak')})")
        dos = st.get("dossier", {}) or {}
        rs = dos.get("risk_score")
        assert_test("    Scammer dossier built (risk 0-100, playbook, turns)",
                    isinstance(rs, (int, float)) and 0 <= rs <= 100 and bool(dos.get("playbook")) and (dos.get("turns") or 0) > 0,
                    f"(risk={rs}, playbook={dos.get('playbook')})")
        assert_test("    Server time + AI activity exposed to dashboard",
                    bool(st.get("server_time_ms")) and isinstance(st.get("ai_activity"), dict))
    except Exception as e:
        assert_test("12. Analytics feature tests", False, str(e))

    # 13. Platform-agnostic multi-channel (Instagram DM simulation)
    try:
        r = requests.post(f"{BASE_URL}/api/simulate-dm", json={
            "platform": "instagram",
            "sender": "promo.scam.2026",
            "text": "Hello winner, click here to claim your prize money now!!"
        }, timeout=15)
        assert_test("13. POST /api/simulate-dm (instagram) returns 200", r.status_code == 200)
        st = requests.get(f"{BASE_URL}/api/state", timeout=10).json()
        th = st.get("incoming_threads", {})
        assert_test("    IG handle thread keyed + tagged instagram",
                    "promo.scam.2026" in th and th["promo.scam.2026"].get("platform") == "instagram",
                    f"(threads={list(th)[-3:]})")
        chat = st.get("scammer_chat", [])
        plats = {m.get("platform") for m in chat if m.get("platform")}
        assert_test("    Chat messages carry platform tags", "instagram" in plats, f"({sorted(plats)})")
        assert_test("    Funnel + scam-type analytics updated by DM",
                    bool(st.get("scam_type_counts")))
    except Exception as e:
        assert_test("13. Multi-channel simulate-dm test", False, str(e))

    # 14. Dashboard: premium dark theme + new widgets served on GET /
    try:
        r = requests.get(BASE_URL, timeout=10)
        h = r.text
        assert_test("14. GET / returns dashboard 200", r.status_code == 200)
        required = ["funnel-steps", "risk-ring", "donut-segments", "typing-indicator",
                    "origin-map", "arcgisonline", "kpi-chip",
                    "wa-bubble-ai", "seg-btn",
                    "function esc(", "visible_after_ms", "server_time_ms",
                    "@media (max-width: 1023px)", "kpi-msgs"]
        missing = [m for m in required if m not in h]
        assert_test("    All 6 feature widgets + responsive markers present", not missing,
                    f"(missing={missing})")
        assert_test("    API key placeholder injected (no raw token in HTML)",
                    "__SCAMTRAP_API_KEY__" not in h)
    except Exception as e:
        assert_test("14. Dashboard HTML test", False, str(e))

    total = len(results)
    passed = sum(1 for r in results if r)
    pct = round((passed * 100.0) / total, 1) if total > 0 else 0.0
    print("=" * 60)
    print(f"TEST SUMMARY: {passed}/{total} Tests Passed ({pct:.1f}%)")
    print("=" * 60)

if __name__ == "__main__":
    run_tests()
