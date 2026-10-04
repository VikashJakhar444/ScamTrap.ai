"""
ScamTrap AI - "Every Situation" Conversation Test Suite (no server required).

Drives the full inbound pipeline (dedup -> IOC extraction -> brain -> staged
payload gate -> human delivery plan) across the situations a scammer uses to
expose a bot:

  * off-script messages ("I don't want money to check")
  * bot tests (math, "repeat what I said", "are you a bot", OTP asks)
  * persona consistency across turns
  * staged payload discipline (screenshot first, canary link only later, max 3)
  * language mirroring (English scammer -> English reply)
  * human timing (read delay, typing speed, bubble splits, screenshot flow)
  * no repetition, no self-reveal, no payment talk in casual chat

Modes:
  python test_conversation.py                 # Gemini brain (default)
  set FORCE_FALLBACK=1 && python test_conversation.py   # deterministic brain
"""

import os
import re
import sys
import uuid
import base64
import zlib
# Add project root to sys.path so app and human_engine can be imported
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import app            # noqa: E402  (module import runs init)
import human_engine   # noqa: E402

if os.environ.get("FORCE_FALLBACK") == "1":
    app.client = None
    app.GEMINI_API_KEY = ""

BRAIN = "DETERMINISTIC FALLBACK" if not app.client else "GEMINI + RULE GATE"
NUM = "919812345678"

from starlette.requests import Request  # noqa: E402

results = []


def assert_test(name, condition, details=""):
    passed = bool(condition)
    results.append(passed)
    print(f"[{'PASS' if passed else 'FAIL'}] {name} {details}", flush=True)
    return passed


def fake_request():
    return Request({
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": "/api/wa-incoming", "raw_path": b"/api/wa-incoming",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 41234),
    })


def send(text, trap=True, msg_id=None):
    if trap:
        app.STATE["trapped_numbers"].add(NUM)
    return app.handle_wa_incoming(fake_request(), {
        "msg_id": msg_id or f"MSG_{uuid.uuid4().hex[:12]}",
        "sender_jid": f"{NUM}@c.us",
        "sender_number": NUM,
        "text": text,
    })


def fresh():
    app.reset_session()


# Payment-bait phrases that must NEVER appear in a casual-chat reply.
BAIT_MARKERS = [
    "kisme bhej", "upi id", "gpay khol", "phonepe", "account de", "ifsc",
    "transfer karta", "im?ps", "where should i send", "share the upi",
    "account number", "send me the upi", "pay karo", "bhej do",
]
HINDI_MARKERS = ["bhai", "yaar", "arre", "acha ", "theek", "chalo"]


def no_bait(reply):
    low = (reply or "").lower()
    return not any(m in low for m in BAIT_MARKERS)


def no_self_reveal(reply):
    return app._SELF_REVEAL_RE.search(reply or "") is None


def check_turn(turn, tag):
    """Run one inbound message and assert everything we can about the reply."""
    msg = turn["msg"]
    resp = send(msg)

    assert_test(f"{tag}: HTTP pipeline returned TRAPPED", resp.get("status") == "TRAPPED", f"({msg[:45]!r})")
    if resp.get("status") != "TRAPPED":
        return None

    reply = (resp.get("reply_text") or "").strip()
    plan = resp.get("delivery_plan")

    # 1. Intent classification matches the situation
    if turn.get("intent"):
        got = app.classify_intent(msg)
        assert_test(f"{tag}: intent is {turn['intent']}", got == turn["intent"], f"(got {got})")

    # 2. Reply exists, is sane, and never reveals the machine
    assert_test(f"{tag}: reply exists and is <=500 chars", bool(reply) and len(reply) <= 500, f"({len(reply)} chars)")
    assert_test(f"{tag}: no AI/bot self-reveal", no_self_reveal(reply), f"-> {reply[:70]!r}")
    assert_test(f"{tag}: reply is not a repeated previous reply", reply not in app.STATE["session"].get("recent_replies", [])[:-1],
                f"-> {reply[:60]!r}")

    # 3. Human delivery plan: never instant, typing matches a human thumb
    assert_test(f"{tag}: delivery_plan present", isinstance(plan, list) and len(plan) > 0)
    issues = human_engine.validate_plan(plan or [])
    assert_test(f"{tag}: plan passes human-timing validation", issues == [], f"{issues}")
    assert_test(f"{tag}: recommended_delay_ms = plan time-to-first-send",
                resp.get("recommended_delay_ms") == human_engine.plan_time_to_first_send_ms(plan or []),
                f"({resp.get('recommended_delay_ms')}ms)")

    # 3b. Time management: model thinking time is absorbed into the human
    # delay, so typing NEVER starts before 3s - even for a fast model,
    # and even when Gemini itself took many seconds.
    model_ms = resp.get("model_ms")
    total_ms = resp.get("total_delay_ms")
    if model_ms is not None and total_ms is not None:
        assert_test(f"{tag}: total delay = model time + plan delay",
                    total_ms == model_ms + (resp.get("recommended_delay_ms") or 0),
                    f"({model_ms}+{resp.get('recommended_delay_ms')}ms)")
        assert_test(f"{tag}: total human delay >= 3000ms", total_ms >= 3000, f"({total_ms}ms)")
        if plan and plan[0].get("action") == "wait":
            typing_starts = model_ms + int(plan[0]["ms"])
            assert_test(f"{tag}: typing starts >= 1500ms after the message",
                        typing_starts >= 1500, f"({typing_starts}ms)")

    # 4. Payload staging rules
    expect_media = turn.get("media")
    expect_link = turn.get("link")
    if expect_media is not None:
        assert_test(f"{tag}: media={'expected' if expect_media else 'forbidden'}",
                    bool(resp.get("media_base64")) == bool(expect_media))
    if expect_link is not None:
        assert_test(f"{tag}: link={'expected' if expect_link else 'forbidden'}",
                    bool(resp.get("trap_url")) == bool(expect_link))
    if resp.get("trap_url"):
        assert_test(f"{tag}: canary URL actually appears in reply text",
                    resp["trap_url"] in reply)
    if resp.get("media_base64"):
        assert_test(f"{tag}: screenshot caption tells the failure story",
                    any(w in reply.lower() for w in ["screenshot", "fail", "error", "limit", "hold", "u16"]),
                    f"-> {reply[:70]!r}")

    # 5. Casual replies must not run the payment script
    if turn.get("casual"):
        assert_test(f"{tag}: casual reply contains no payment bait", no_bait(reply), f"-> {reply[:70]!r}")

    # 6. Language mirroring
    if turn.get("english"):
        low = (reply or "").lower()
        assert_test(f"{tag}: English input -> no Hinglish markers",
                    not any(m.strip() in low.split() or m in low for m in ["bhai", "yaar"]),
                    f"-> {reply[:70]!r}")

    if turn.get("extra"):
        turn["extra"](reply, resp)

    return resp


# ---------------------------------------------------------------------
# Named extra checks
# ---------------------------------------------------------------------
def math_answered(reply, resp):
    assert_test("math: 7*8 answered as 56", "56" in reply, f"-> {reply[:70]!r}")


def otp_refused(reply, resp):
    assert_test("otp: no 6-digit code leaked", re.search(r"\b\d{6}\b", reply) is None, f"-> {reply[:70]!r}")


def bait_asked(reply, resp):
    low = reply.lower()
    assert_test("bait: reply engages the scam/payment premise",
                any(w in low for w in ["upi", "pay", "bhej", "send", "gpay", "phonepe",
                                       "paisa", "paise", "kitna", "charge", "fine",
                                       "detail", "bata", "kaise", "bill",
                                       "what", "tell", "kya"]),
                f"-> {reply[:70]!r}")


# ---------------------------------------------------------------------
# Scenario matrix
# ---------------------------------------------------------------------
SCENARIOS = [
    # --- casual / anti-script situations ---
    dict(name="greeting", msg="hello", intent="greeting", casual=True, media=False, link=False),
    dict(name="how_are_you", msg="kaise hai aaj kal?", intent="how_are_you", casual=True, media=False, link=False),
    dict(name="identity_probe", msg="kaun ho tum?", intent="identity", casual=True, media=False, link=False),
    dict(name="wrong_number", msg="wrong number lagta hai", intent="wrong_number", casual=True, media=False, link=False),
    dict(name="refusal_no_money", msg="i don't want money to check, just verify the status",
         intent="refusal", casual=True, media=False, link=False),
    dict(name="bot_suspicion", msg="are you a bot? answer fast", intent="suspicion", casual=True, media=False, link=False),
    dict(name="speed_suspicion", msg="you are typing too fast, are you an AI?",
         intent="suspicion", casual=True, media=False, link=False),
    dict(name="math_test", msg="what is 7 * 8?", intent="test_challenge", casual=True, media=False, link=False,
         extra=math_answered),
    dict(name="repeat_test", msg="repeat what I said in the last message",
         intent="test_challenge", casual=True, media=False, link=False),
    dict(name="otp_ask", msg="send me the OTP you received, quickly",
         intent="otp_ask", media=False, link=False, extra=otp_refused),
    dict(name="voice_video_call", msg="video call kar lete hai abhi", intent="call_ask", casual=True, media=False, link=False),
    dict(name="photo_request", msg="apni photo bhej de ek", intent="photo_ask", casual=True, media=False, link=False),
    dict(name="goodbye", msg="ok bye good night", intent="goodbye", casual=True, media=False, link=False),
    dict(name="hurry_abuse", msg="abe jaldi kar kyu time waste kar raha hai",
         intent="hurry_abuse", casual=True, media=False, link=False),
    dict(name="social_invite", msg="chal kal college chalte hai", intent="social", casual=True, media=False, link=False),
    dict(name="scammer_sends_link", msg="open this http://bit.ly/know-more",
         intent="link_received", casual=True, media=False, link=False),
    dict(name="emoji_only", msg="\U0001F602\U0001F602", intent="fallback", casual=True, media=False, link=False),

    # --- scam scripts: bait without payload ---
    dict(name="bill_scam_bait", msg="Your electricity bill of Rs 25000 is pending. Pay now or connection will be cut!",
         intent="scam_bill", media=False, link=False, extra=bait_asked),
    dict(name="job_scam_bait", msg="part time job offer, earn Rs 500 daily, just pay registration fee 500",
         intent="scam_job", media=False, link=False, extra=bait_asked),
    dict(name="police_threat_bait", msg="CBI police will arrest you, pay Rs 10000 fine immediately",
         intent="scam_threat", media=False, link=False, extra=bait_asked),
    dict(name="english_scam", msg="Please pay the pending fee of Rs 500 today to complete your KYC",
         intent="scam_threat", english=True, media=False, link=False, extra=bait_asked),

    # --- staged payment flows (multi-turn) ---
    dict(name="staged_uip_flow", script=[
        dict(msg="Send ₹25000 to electricity.discom@sbi right now or light will be cut!",
             intent="payment_details", media=True, link=False),
        dict(msg="abhi tak nahi aaya, jaldi check kar!",
             intent="money_demand", media=False, link=True),
    ]),
    dict(name="bank_details_first", script=[
        dict(msg="account number 123456789012 IFSC HDFC0001234, transfer fast",
             intent="payment_details", media=False, link=True),
    ]),

    # --- persona cannot be trapped ---
    dict(name="persona_consistency", script=[
        dict(msg="who are you? apna naam bata", intent="identity", casual=True),
        dict(msg="what is your name again? tell me properly", intent="identity", casual=True),
    ]),
]


def persona_check(prev_replies):
    """Both identity answers must agree with the stored persona and never
    mention a different persona name."""
    name = (app.STATE.get("persona") or {}).get("name", "").lower()
    text = " ".join(prev_replies).lower()
    others = [n.lower() for n in app.PERSONA_FIRST_NAMES if n.lower() != name]
    mentioned_others = [n for n in others if re.search(rf"\b{re.escape(n)}\b", text)]
    assert_test("persona: stored name is mentioned in identity answers",
                bool(name) and name in text, f"(name={name}, answers={prev_replies})")
    assert_test("persona: no other persona name leaks", not mentioned_others, f"{mentioned_others}")


def run_scenarios():
    for sc in SCENARIOS:
        fresh()
        print(f"\n--- SCENARIO: {sc['name']} ---", flush=True)
        turns = sc.get("script") or [sc]
        replies = []
        for i, turn in enumerate(turns, 1):
            resp = check_turn(turn, f"{sc['name']}#{i}")
            if resp:
                replies.append(resp.get("reply_text") or "")
        if sc["name"] == "persona_consistency" and len(replies) == 2:
            persona_check(replies)


def run_repetition_test():
    fresh()
    print("\n--- SCENARIO: repetition_resistance ---", flush=True)
    replies = []
    for i in range(5):
        resp = send(f"hello")
        if resp.get("reply_text"):
            replies.append(resp["reply_text"])
    assert_test("repetition: 5 identical pings produced >=3 distinct replies",
                len(set(replies)) >= 3, f"({len(set(replies))} distinct of {len(replies)})")


def run_soak_test():
    """24-turn mixed conversation - invariants must hold on every single turn."""
    fresh()
    print("\n--- SCENARIO: soak_24_turn_invariants ---", flush=True)
    script = [
        ("hello", True),
        ("kaun ho tum?", True),
        ("Your electricity bill of Rs 25000 is pending. Pay now or power will be cut!", False),
        ("Send ₹25000 to electricity.discom@sbi right now!", False),
        ("are you a bot?", True),
        ("abhi tak nahi aaya, check kar", False),
        ("who are you again?", True),
        ("what is 7 * 8?", True),
        ("ok bye good night", True),
        ("payment karo abhi turant", False),
        ("ok done", True),
        ("apni photo bhej de", True),
        ("nahi aaya paisa, jaldi kar", False),
        ("chal kal milte hai", True),
        ("wrong number hai shayad", True),
        ("kaise hai bhai", True),
        ("part time job hai, registration fee 500 bhejo", False),
        ("send to job.upi@okaxis now", False),
        ("still pending? verify kar jaldi", False),
        ("repeat what I said", True),
        ("chal abhi", True),
        ("bill katne wala hai, Rs 999 pay karo", False),
        ("hello", True),
        ("thanks bye", True),
    ]
    total_links = 0
    total_media = 0
    prev_reply = None
    for i, (msg, casual) in enumerate(script, 1):
        resp = send(msg)
        if resp.get("status") != "TRAPPED":
            assert_test(f"soak#{i}: TRAPPED", False, f"({msg!r})")
            continue
        reply = (resp.get("reply_text") or "").strip()
        plan = resp.get("delivery_plan") or []

        issues = human_engine.validate_plan(plan)
        assert_test(f"soak#{i}: human plan valid ({msg[:28]!r})", issues == [], f"{issues}")
        assert_test(f"soak#{i}: no self-reveal", no_self_reveal(reply))
        if casual:
            assert_test(f"soak#{i}: casual stays off-script", no_bait(reply), f"-> {reply[:60]!r}")
        if prev_reply is not None:
            assert_test(f"soak#{i}: not identical to previous reply", reply != prev_reply)
        if resp.get("trap_url"):
            assert_test(f"soak#{i}: link text present", resp["trap_url"] in reply)
        total_links += 1 if resp.get("trap_url") else 0
        total_media += 1 if resp.get("media_base64") else 0
        prev_reply = reply

    assert_test("soak: canary links capped at 3 for the whole session", total_links <= 3, f"({total_links})")
    assert_test("soak: failure screenshot sent at most once per session", total_media <= 1, f"({total_media})")
    assert_test("soak: screenshot was actually sent in the payment phase", total_media == 1, f"({total_media})")
    assert_test("soak: at least one canary link was delivered", total_links >= 1, f"({total_links})")


def run_pipeline_tests():
    fresh()
    print("\n--- SCENARIO: pipeline_guards ---", flush=True)

    actual_phone, is_lid = app._normalize_sender_number("6204257765", "213142500053021@lid")
    assert_test("LID resolution: known attacker phone normalizes to +91 6204257765",
                actual_phone == "916204257765" and is_lid,
                f"(got {actual_phone}, is_lid={is_lid})")
    unresolved_phone, _ = app._normalize_sender_number(
        "213142500053021", "213142500053021@lid")
    assert_test("LID fallback: opaque WhatsApp identifier is not treated as a phone",
                unresolved_phone == "LID:213142500053021",
                f"(got {unresolved_phone})")

    fresh()
    app.handle_wa_incoming(fake_request(), {
        "msg_id": "MSG_FIR_ATTACKER_PHONE",
        "sender_jid": "213142500053021@lid",
        "sender_number": "6204257765",
        "text": "Please send payment urgently."
    })
    pdf = app.generate_fir_pdf().getvalue()
    report_streams = []
    for match in re.finditer(rb"stream\r?\n", pdf):
        end = pdf.find(b"endstream", match.end())
        if end < 0:
            continue
        encoded = pdf[match.end():end].strip().removesuffix(b"~>")
        try:
            report_streams.append(zlib.decompress(base64.a85decode(encoded)))
        except (ValueError, zlib.error):
            continue
    assert_test("FIR generation: report identifies attacker as +916204257765",
                any(b"+916204257765" in stream for stream in report_streams))
    assert_test("FIR generation: report excludes opaque WhatsApp LID",
                all(b"+213142500053021" not in stream for stream in report_streams))

    fresh()
    app.handle_wa_incoming(fake_request(), {
        "msg_id": "MSG_FIR_UNRESOLVED_LID",
        "sender_jid": "213142500053021@lid",
        "sender_number": "",
        "text": "Message from a contact whose number is unavailable."
    })
    unresolved_pdf = app.generate_fir_pdf().getvalue()
    unresolved_streams = []
    for match in re.finditer(rb"stream\r?\n", unresolved_pdf):
        end = unresolved_pdf.find(b"endstream", match.end())
        if end < 0:
            continue
        encoded = unresolved_pdf[match.end():end].strip().removesuffix(b"~>")
        try:
            unresolved_streams.append(zlib.decompress(base64.a85decode(encoded)))
        except (ValueError, zlib.error):
            continue
    assert_test("Unresolved LID: no fabricated attacker phone is selected",
                app.STATE["target_scammer"] is None)
    assert_test("Unresolved LID: FIR reports multi-target, not LID digits",
                any(b"Multi-Target Intercept" in stream for stream in unresolved_streams)
                and all(b"+213142500053021" not in stream for stream in unresolved_streams))
    command_result = app.handle_bot_self_command(fake_request(), {
        "text": "trap",
        "last_incoming_from": "213142500053021@lid"
    })
    assert_test("Unresolved LID: bare trap command does not select LID as attacker phone",
                command_result.get("status") == "error"
                and app.STATE["target_scammer"] is None)

    fresh()
    resolved_command = app.handle_bot_self_command(fake_request(), {
        "text": "trap",
        "last_incoming_from": "6204257765"
    })
    assert_test("Resolved LID: bare trap command targets the real attacker phone",
                resolved_command.get("status") == "ok"
                and app.STATE["target_scammer"] == "+916204257765")

    fresh()
    # Standby: unknown number gets no auto-reply
    resp = send("hello there", trap=False)
    assert_test("standby: unknown sender is not replied to",
                resp.get("status") == "STANDBY" and not resp.get("should_reply"))

    # Dedup: same msg_id twice
    app.STATE["trapped_numbers"].add(NUM)
    mid = f"MSG_{uuid.uuid4().hex[:12]}"
    first = send("hello", msg_id=mid)
    second = send("hello", msg_id=mid)
    assert_test("dedup: first delivery processed", first.get("status") == "TRAPPED")
    assert_test("dedup: replayed msg_id ignored", second.get("status") == "DUPLICATE_IGNORED")

    # State exposes persona + session for the dashboard
    st = app.get_state()
    import json
    body = json.loads(st.body)
    assert_test("state: persona and session exposed",
                "persona" in body and "session" in body and "stage" in body["session"])


def run_human_engine_tests():
    print("\n--- UNIT: human_engine ---", flush=True)

    # Bubble splitting
    long_text = "Bhai ye dekh screenshot " * 20
    bubbles = human_engine.split_bubbles(long_text)
    assert_test("split: long text becomes 1-3 bubbles", 1 <= len(bubbles) <= 3, f"({len(bubbles)})")
    assert_test("split: no bubble is a giant wall", all(len(b) <= 300 for b in bubbles),
                f"{[len(b) for b in bubbles]}")
    assert_test("split: short text stays one bubble", len(human_engine.split_bubbles("ok bhai")) == 1)

    # Typing speed scales with length (thumb typing, never instant)
    short_samples = [human_engine.typing_ms("ok") for _ in range(30)]
    long_samples = [human_engine.typing_ms("Bhai maine try kar liya par bank ka daily limit error aa raha hai, "
                                           "screenshot bhej raha hu tu dekh le ek baar") for _ in range(30)]
    assert_test("typing: short reply >= 1.9s", min(short_samples) >= 1900, f"({min(short_samples)}ms)")
    assert_test("typing: long reply takes longer than short on average",
                sum(long_samples) / len(long_samples) > sum(short_samples) / len(short_samples))
    assert_test("typing: long reply <= 45s ceiling", max(long_samples) <= 45000)

    # Read delay never instant
    reads = [human_engine.read_delay_ms(prompt_len=120) for _ in range(50)]
    assert_test("read: delay >= 1500ms before typing even starts", min(reads) >= 1500, f"({min(reads)}ms)")
    assert_test("read: delay <= 18s ceiling", max(reads) <= 18000)

    # Time management: model thinking time is absorbed into the read delay
    txt = "Haan bhai, abhi dekh ke batata hu tu ruk."
    fast = human_engine.build_delivery_plan(txt, elapsed_ms=0)
    slow = human_engine.build_delivery_plan(txt, elapsed_ms=2500)
    absorbed = human_engine.build_delivery_plan(txt, elapsed_ms=60000)
    assert_test("absorb: fast model still waits >= 1.5s before typing",
                fast[0]["ms"] >= human_engine.READ_FLOOR_MS, f"({fast[0]['ms']}ms)")
    assert_test("absorb: model time shrinks the planned wait",
                slow[0]["ms"] <= human_engine.READ_MAX_MS - 2500 and slow[0]["ms"] >= human_engine.RESIDUAL_WAIT_MS,
                f"({slow[0]['ms']}ms for elapsed=2500)")
    assert_test("absorb: huge model time floors at the residual wait",
                absorbed[0]["ms"] == human_engine.RESIDUAL_WAIT_MS, f"({absorbed[0]['ms']}ms)")
    assert_test("absorb: typing still starts >= 1.5s after the message",
                fast[0]["ms"] >= 1500, f"({fast[0]['ms']}ms)")
    assert_test("absorb: absorbed plan still validates clean",
                human_engine.validate_plan(slow) == [], f"{human_engine.validate_plan(slow)}")

    # Text plan shape
    plan = human_engine.build_delivery_plan(
        "Bhai {amt} bhej diya par daily limit error aa gaya. Screenshot dekh aur dusra account de do jaldi please bhai.",
        media=False, prompt_len=80)
    assert_test("plan: text plan starts with a read delay", plan and plan[0]["action"] == "wait")
    assert_test("plan: text plan sends >=1 bubble", any(s["action"] == "send" for s in plan))
    assert_test("plan: no zero/negative durations",
                all(s.get("ms", 1) > 0 for s in plan if s["action"] in ("wait", "typing")))
    assert_test("plan: validates clean", human_engine.validate_plan(plan) == [],
                f"{human_engine.validate_plan(plan)}")

    # Media plan follows the app-switch -> screenshot -> caption narrative
    mplan = human_engine.build_delivery_plan("payment fail ho gaya, dekh", media=True)
    assert_test("plan: media plan ends with send_media",
                mplan and mplan[-1]["action"] == "send_media")
    assert_test("plan: media plan waits for app switch + screenshot (>=10s) before sending",
                human_engine.plan_time_to_first_send_ms(mplan) >= 10000,
                f"({human_engine.plan_time_to_first_send_ms(mplan)}ms)")
    assert_test("plan: media plan shows typing for the caption",
                any(s["action"] == "typing" for s in mplan))

    # Media plan absorbs model time into the app-switch narrative
    mslow = human_engine.build_delivery_plan("payment fail ho gaya, dekh", media=True, elapsed_ms=60000)
    assert_test("plan: media absorbs huge model time into app-switch wait",
                mslow[0]["ms"] == human_engine.RESIDUAL_WAIT_MS, f"({mslow[0]['ms']}ms)")
    assert_test("plan: absorbed media plan still validates clean",
                human_engine.validate_plan(mslow) == [], f"{human_engine.validate_plan(mslow)}")


def main():
    print("=" * 64)
    print(f"SCAMTRAP AI CONVERSATION SITUATION SUITE - brain: {BRAIN}")
    print("=" * 64)

    run_human_engine_tests()
    run_scenarios()
    run_repetition_test()
    run_pipeline_tests()
    run_soak_test()

    total = len(results)
    passed = sum(1 for r in results if r)
    pct = round((passed * 100.0) / total, 1) if total else 0.0
    print("=" * 64)
    print(f"SUMMARY: {passed}/{total} checks passed ({pct:.1f}%) - brain: {BRAIN}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
