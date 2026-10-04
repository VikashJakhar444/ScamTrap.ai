"""
ScamTrap AI - Human Behaviour Engine

Models how a real person actually behaves on WhatsApp so the honeypot can
never be fingerprinted as a bot:

1. READ DELAY     - nobody answers the instant a message lands. A human picks
                    up the phone, reads, thinks, then starts typing.
2. TYPING SPEED   - thumb typing on a phone runs roughly 45-100 ms per
                    character with extra hesitation between words, so long
                    messages still take a few seconds, never 1.
3. BUBBLE SPLIT   - humans fire off 1-3 short messages, not one giant wall
                    of text.
4. PAUSES         - the typing indicator flickers off mid-message while the
                    person re-reads or gets distracted.
5. SCREENSHOT FLOW- sending a "payment failed" screenshot means leaving
                    WhatsApp, attempting the transaction, screenshotting the
                    error, coming back and typing a caption.

Every reply is compiled into a *delivery plan*: an ordered list of timed
steps the WhatsApp bridge replays verbatim (wait / typing / clear_state /
send / send_media).
"""

import random
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------
# Tunable human ranges (milliseconds unless stated otherwise)
# ---------------------------------------------------------------------
READ_DELAY_RANGE = (1600, 3600)       # pickup + read + think, before typing
READ_FLOOR_MS = 1500                  # a real human never answers instantly
READ_PER_CHAR_MS = 6                  # longer message -> longer read
READ_MAX_MS = 9000
RESIDUAL_WAIT_MS = 350                # wait left after "thinking time" is absorbed

TYPING_BASE_MS = (400, 900)           # unlock, open chat, focus field
TYPING_PER_CHAR_MS = (0.045, 0.10)    # quick but still human thumb typing
TYPING_PER_WORD_MS = (15, 55)         # hesitation between words
TYPING_MIN_MS = 1900
TYPING_MAX_MS = 20000

INTER_BUBBLE_GAP_MS = (600, 1800)     # pause between two messages
MID_PAUSE_CHANCE = 0.15               # typing flickers off mid-message
MID_PAUSE_RANGE = (1200, 2800)

MAX_BUBBLE_CHARS = 110                # one WhatsApp bubble cap
MAX_BUBBLES = 3

# Screenshot ("payment failed") narrative
APP_SWITCH_RANGE = (7000, 11000)      # leave WhatsApp, open GPay/PhonePe, try txn
SCREENSHOT_RANGE = (1500, 3000)       # error pops, screenshot taken, back to WA


def split_bubbles(
    text: str,
    max_chars: int = MAX_BUBBLE_CHARS,
    max_bubbles: int = MAX_BUBBLES,
) -> List[str]:
    """Split a reply the way a human types it: 1-3 short messages split on
    sentence boundaries (falling back to word boundaries for walls of text)."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    parts = [p for p in re.split(r"(?<=[.!?।])\s+", text) if p]

    bubbles: List[str] = []
    cur = ""
    for part in parts:
        cand = f"{cur} {part}".strip() if cur else part
        if len(cand) <= max_chars:
            cur = cand
            continue
        if cur:
            bubbles.append(cur)
            cur = ""
        while len(part) > max_chars:
            cut = part.rfind(" ", 0, max_chars)
            if cut <= 0:
                cut = max_chars
            bubbles.append(part[:cut].strip())
            part = part[cut:].strip()
        cur = part
    if cur:
        bubbles.append(cur)

    # Too many fragments -> merge the smallest adjacent pairs.
    while len(bubbles) > max_bubbles:
        pair_idx, pair_len = 0, None
        for i in range(len(bubbles) - 1):
            total = len(bubbles[i]) + len(bubbles[i + 1])
            if pair_len is None or total < pair_len:
                pair_idx, pair_len = i, total
        merged = f"{bubbles[pair_idx]} {bubbles[pair_idx + 1]}".strip()
        bubbles[pair_idx:pair_idx + 2] = [merged]

    return [b for b in bubbles if b]


def read_delay_ms(prompt_len: int = 0, pace: float = 1.0) -> int:
    """Delay between the scammer's message arriving and the typing indicator
    starting (phone pickup, reading, thinking). Always >= ~3s: nobody
    answers instantly, and the model's own thinking time lives inside this
    window (see build_delivery_plan(elapsed_ms=...))."""
    base = random.uniform(*READ_DELAY_RANGE) + min(int(prompt_len or 0), 400) * READ_PER_CHAR_MS
    # 10% of the time the victim is distracted (busy, away, reading something else)
    if random.random() < 0.10:
        base += random.uniform(2500, 7000)
    return int(max(READ_FLOOR_MS * pace, min(base * pace, READ_MAX_MS)))


def absorb_elapsed(target_ms: float, elapsed_ms: int) -> int:
    """Subtract time already spent (Gemini generating the reply) from a planned
    wait. The scammer only sees wall-clock time: if the model took 4s of a 4.5s
    read delay, only the remaining ~0.5s has to be planned - but the typing
    indicator must still never start before the human read floor."""
    return int(max(RESIDUAL_WAIT_MS, target_ms - max(0, int(elapsed_ms))))


def typing_ms(text: str, pace: float = 1.0) -> int:
    """How long the typing indicator stays on before a bubble is sent."""
    text = (text or "").strip()
    if not text:
        return 0
    ms = random.uniform(*TYPING_BASE_MS)
    ms += len(text) * random.uniform(*TYPING_PER_CHAR_MS) * 1000
    ms += len(text.split()) * random.uniform(*TYPING_PER_WORD_MS)
    ms *= pace
    # Occasional "confused pause" while composing a tricky sentence
    if len(text) > 60 and random.random() < 0.15:
        ms += random.uniform(1500, 4000)
    return int(max(TYPING_MIN_MS, min(ms, TYPING_MAX_MS)))


def plan_time_to_first_send_ms(plan: List[Dict[str, Any]]) -> int:
    """Total elapsed time from incoming message to the first outgoing send."""
    total = 0
    for step in plan:
        if step.get("action") in ("send", "send_media"):
            return total
        total += int(step.get("ms") or 0)
    return total


def build_delivery_plan(
    text: str,
    media: bool = False,
    prompt_len: int = 0,
    pace: float = 1.0,
    elapsed_ms: int = 0,
) -> List[Dict[str, Any]]:
    """Compile a reply into the timed step list replayed by wa_bridge.js.

    elapsed_ms = wall-clock time already spent since the scammer's message
    arrived (mainly the model generating this reply). It is counted as part
    of the human read/app-switch delay so total reply time stays human even
    when Gemini is slow - and never starts typing before READ_FLOOR_MS.

    Text reply flow:
        wait(read) -> typing -> send -> [clear_state -> wait(flicker)] -> send ...
    Screenshot reply flow (narrative: victim tried the payment, it failed,
    they screenshotted the error and came back to WhatsApp to explain):
        wait(app switch + failed txn) -> wait(screenshot) -> typing(caption)
        -> send_media
    """
    text = (text or "").strip()
    plan: List[Dict[str, Any]] = []

    if media:
        app_switch = absorb_elapsed(random.uniform(*APP_SWITCH_RANGE) * pace, elapsed_ms)
        plan.append({"action": "wait", "ms": app_switch})
        plan.append({"action": "wait", "ms": int(random.uniform(*SCREENSHOT_RANGE) * pace)})
        plan.append({"action": "typing", "ms": typing_ms(text, pace)})
        plan.append({"action": "send_media", "caption": text})
        return plan

    bubbles = split_bubbles(text)
    if not bubbles:
        return plan

    plan.append({"action": "wait", "ms": absorb_elapsed(read_delay_ms(prompt_len, pace), elapsed_ms)})

    for i, bubble in enumerate(bubbles):
        planned_typing = typing_ms(bubble, pace)
        # Typing indicator flickers off mid-way while the person re-reads.
        # Only worth splitting when both halves still read as human typing.
        first = second = 0
        if len(bubble) > 70 and planned_typing >= 4200 and random.random() < MID_PAUSE_CHANCE:
            first = max(1800, int(planned_typing * random.uniform(0.4, 0.65)))
            second = planned_typing - first
        if first and second >= 1800:
            plan.append({"action": "typing", "ms": first})
            plan.append({"action": "clear_state"})
            plan.append({"action": "wait", "ms": int(random.uniform(*MID_PAUSE_RANGE))})
            plan.append({"action": "typing", "ms": second})
        else:
            plan.append({"action": "typing", "ms": planned_typing})
        plan.append({"action": "send", "text": bubble})
        if i < len(bubbles) - 1:
            plan.append({"action": "clear_state"})
            plan.append({"action": "wait", "ms": int(random.uniform(*INTER_BUBBLE_GAP_MS))})

    return plan


def validate_plan(plan: List[Dict[str, Any]]) -> List[str]:
    """Return a list of human-likeness violations (empty list == passes).
    Used by the test suite to prove no bot-like timing can escape."""
    issues: List[str] = []
    if not plan:
        issues.append("empty delivery plan")
        return issues

    allowed = {"wait", "typing", "clear_state", "send", "send_media"}
    first_send_seen = False
    total = 0

    for idx, step in enumerate(plan):
        action = step.get("action")
        if action not in allowed:
            issues.append(f"step {idx}: unknown action {action!r}")
            continue
        if action in ("wait", "typing"):
            ms = step.get("ms")
            if not isinstance(ms, int) or ms <= 0:
                issues.append(f"step {idx}: non-positive duration {ms!r}")
                continue
            total += ms
            if action == "wait" and ms > 120000:
                issues.append(f"step {idx}: wait {ms}ms is unrealistically long")
            if action == "typing" and ms < 1800:
                issues.append(f"step {idx}: typing {ms}ms faster than a human thumb")
            if action == "typing" and ms > 60000:
                issues.append(f"step {idx}: typing {ms}ms held too long")
        if action in ("send", "send_media") and not first_send_seen:
            first_send_seen = True
            if total < 2500:
                issues.append(f"first reply after {total}ms - instant bot behaviour")

    if not first_send_seen:
        issues.append("plan never sends anything")
    return issues
