import html
import re
import time
import threading
from datetime import datetime, timezone
from google import genai
from config import LLM_API_KEY
from notifier import send_alert, send_text

_MODEL = "gemini-3.1-flash-lite"
_client = genai.Client(api_key=LLM_API_KEY)
print(f"[LLM] key={LLM_API_KEY[:8]}… model={_MODEL}")

# Rate limiter: free tier = 15 RPM; 5 s gap → 12 RPM to avoid edge-of-window 429s
_lock = threading.Lock()
_last_call_time = 0.0
_MIN_INTERVAL = 5.0
_LABEL_RE = re.compile(r"^\s*line\s*\d+\s*:\s*", re.I)

# A 429 already survives up to 2 in-call retries with backoff before giving
# up; other errors (bad key, network drop, model unavailable) don't retry
# at all. Requiring 2 separate failed calls before alerting avoids a false
# alarm from one non-429 blip, while still catching a real outage fast.
_LLM_ERROR_ALERT_THRESHOLD = 2
_consecutive_llm_errors = 0
_llm_error_alerted = False

# The process itself can be perfectly healthy — connected to Telegram, pinging
# healthchecks.io on schedule — while the classifier is the thing that's broken
# (quota exhausted, provider outage). healthchecks can't see that: it only
# watches for a heartbeat, and this failure doesn't stop the heartbeat. So this
# is the one channel-facing outage notice that has to come from inside the
# process rather than from an external ping. It uses the same 15-minute bar as
# the dead man's switch, but measured differently by necessity: this is only
# ever checked when a post actually reaches the classifier, so on a quiet day
# with no matching posts, an outage can go undetected past 15 minutes — there's
# no free way to poll the LLM just to test it without spending quota on it.
_LLM_CHANNEL_ALERT_AFTER = 15 * 60
_llm_outage_since: datetime | None = None
_llm_channel_alerted = False

# Kept in sync with the wording used in healthchecks.io's webhook integration
# (see README) — same message either way, whether the tracker is fully down
# or just its classifier, since either way a subscriber's advice is the same:
# "don't trust the channel's silence right now."
CHANNEL_DOWN_MESSAGE = "⚠️ Бот тимчасово не працює"
CHANNEL_UP_MESSAGE = "✅ Бот знову працює"

# Broad stems, matched as substrings: "мобілізац" already covers
# "мобілізація/мобілізаційний/демобілізація", "призов" covers
# "призовник/призовний вік", "кордон" covers "закордонний/прикордонний".
# The stage is meant to over-match — the LLM removes the false positives —
# so prefer a shorter stem over a longer phrase it already contains.
KEYWORDS = [
    # Mobilization
    "мобілізац", "мобілізов", "мобілізуват",
    # Draft / conscription / service obligation
    "призов", "призивн", "повістк", "строков служб", "військовозобов",
    "військовий обов", "військового обов", "військовому обов",
    "військова служб", "військової служб", "військову служб",
    "військовий облік", "військового обліку",
    "військова підготовк", "військової підготовк", "бзвп",
    # Deferral / exemption / booking
    "відстроч", "броню", "заброньован", "непридатн", "обмежено придатн",
    # Age thresholds named in words rather than digits
    "віковий ценз", "знизити вік", "зниження віку", "підвищити вік",
    "підвищення віку", "юнак", "молоді чоловік", "молодих чоловік",
    # Border / exit rules
    "кордон", "виїзд", "виїхат", "виїжджат", "невиїзд", "трудовий фронт",
    "трудового фронт", "трудовому фронт",
    # Institutions
    "тцк", "військкомат", "комісаріат", "міноборони",
    "міністерство оборони", "міністерства оборони", "генштаб",
    # Legal instruments — military-specific only; bare "закон"/"постанова"
    # would match most political news and flood the classifier.
    "№57", "постанова 57", "постанову 57", "указ президента",
    # English terms (for forwarded content)
    "mobilization", "mobilisation", "conscription", "draft exemption",
    "draft age", "military age", "military service", "border crossing",
    "travel ban", "exit ban",
]

# Age brackets go through a regex instead of literal strings. A headline about
# restricting exit for men "18–60" is about this group too, but no literal
# spelled out here would ever have caught it — and the prompt already tells the
# model to accept overlapping brackets, so the gate has to let them through.
# Any bracket overlapping 18–22 counts, in whatever format the post writes it
# ("18-22", "18 – 60", "від 18 до 60", "20 та 27"), as does a single age
# inside the range ("22-річних", "19 років").
_TARGET_MIN, _TARGET_MAX = 18, 22
_PLAUSIBLE_AGES = range(14, 71)  # outside this the digits aren't an age
# A digit flanked by : . , % is a time, decimal or percentage, not an age.
_RANGE_SEP = r"(?:\s*[-–—‒―]\s*|\s*/\s*|\s+(?:до|по|та|і|й|to|and)\s+)"
_AGE_RANGE_RE = re.compile(rf"(?<![\d:.,])(\d{{1,2}}){_RANGE_SEP}(\d{{1,2}})(?![\d:.,%])")
_SINGLE_AGE_RE = re.compile(r"(?<![\d:.,])(\d{1,2})\s*[-–—]?\s*(?:річ|рок|рік|літн|year)")

_PROMPT_TEMPLATE = """\
You are monitoring Ukrainian Telegram news channels for posts relevant to changes \
in Ukrainian military mobilization rules that specifically affect men aged 18–22.

A post is relevant (YES) if it discusses:
- New or changed laws, decrees, or court rulings about mobilization, conscription, \
or draft exemptions for men aged 18–22 (or overlapping brackets that include this group)
- Changes to rules about crossing the Ukrainian border for men aged 18–22
- Changes to БЗВП rules affecting men 18-22
- New deferral or exemption categories that apply to young men aged 18–22
- Official announcements from the Ministry of Defense, TCC (ТЦК), or the President \
specifically about mobilization age brackets

A post is NOT relevant (NO) if it:
- Mentions soldiers, military operations, or the war in general without discussing \
policy/law changes for the 18–22 age group
- Discusses budget, taxes, or parliamentary business not directly related to mobilization policy
- Is about crimes, court cases, or corruption unrelated to mobilization
- Mentions mobilization broadly without specific relevance to the 18–22 age bracket

Channel: {channel}
Post text:
{text}

Answer this question: "Does this post describe a change to exit-abroad rules, \
mobilization rules, or military obligations for Ukrainian men aged 18–22?"

Respond with exactly two lines, with no labels, numbering, or prefixes:
the first line is only the word YES or NO, and the second line is one sentence \
in English explaining why.
"""


def _age_bracket_match(lower: str) -> bool:
    """True if the text names an age range overlapping 18–22, or a single age in it."""
    for a, b in _AGE_RANGE_RE.findall(lower):
        lo, hi = sorted((int(a), int(b)))
        if (lo in _PLAUSIBLE_AGES and hi in _PLAUSIBLE_AGES
                and lo <= _TARGET_MAX and hi >= _TARGET_MIN):
            return True
    return any(_TARGET_MIN <= int(a) <= _TARGET_MAX
               for a in _SINGLE_AGE_RE.findall(lower))


def keyword_match(text: str) -> bool:
    lower = text.lower()
    if any(kw.lower() in lower for kw in KEYWORDS):
        return True
    return _age_bracket_match(lower)


def _note_llm_success() -> None:
    global _consecutive_llm_errors, _llm_error_alerted, _llm_outage_since, _llm_channel_alerted
    _consecutive_llm_errors = 0
    # Any success means the API is not down — a real outage never has one of
    # these in the middle of it. Reset the wall-clock immediately, so a flaky
    # run of alternating success/failure never accumulates into a false alarm.
    _llm_outage_since = None
    if _llm_error_alerted:
        _llm_error_alerted = False
        try:
            send_alert("✅ <b>Classifier recovered</b> — posts are being evaluated normally again.")
        except Exception as e:
            print(f"[LLM] failed to send recovery alert: {e}")
    if _llm_channel_alerted:
        _llm_channel_alerted = False
        try:
            send_text(CHANNEL_UP_MESSAGE)
        except Exception as e:
            print(f"[LLM] failed to send channel recovery notice: {e}")


def _note_llm_failure(reason: str) -> None:
    global _consecutive_llm_errors, _llm_error_alerted, _llm_outage_since, _llm_channel_alerted
    _consecutive_llm_errors += 1
    now = datetime.now(timezone.utc)
    if _llm_outage_since is None:
        _llm_outage_since = now

    if _consecutive_llm_errors >= _LLM_ERROR_ALERT_THRESHOLD and not _llm_error_alerted:
        _llm_error_alerted = True
        try:
            send_alert(
                f"⚠️ <b>CLASSIFIER DOWN</b> — {html.escape(reason)}.\n"
                f"Posts are being logged as NO without real review — "
                f"you may be missing updates."
            )
        except Exception as e:
            print(f"[LLM] failed to send failure alert: {e}")

    if (not _llm_channel_alerted
            and (now - _llm_outage_since).total_seconds() >= _LLM_CHANNEL_ALERT_AFTER):
        _llm_channel_alerted = True
        try:
            send_text(CHANNEL_DOWN_MESSAGE)
        except Exception as e:
            print(f"[LLM] failed to send channel outage notice: {e}")


def llm_classify(text: str, channel: str) -> tuple[bool, str]:
    global _last_call_time
    prompt = _PROMPT_TEMPLATE.format(channel=channel, text=text[:2000])

    # Enforce minimum interval between calls to stay under 15 RPM
    with _lock:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _last_call_time)
        if wait > 0:
            time.sleep(wait)
        _last_call_time = time.monotonic()

    for attempt in range(3):
        try:
            response = _client.models.generate_content(
                model=_MODEL, contents=prompt
            )
            lines = response.text.strip().splitlines()
            # The model intermittently echoes a "Line 1:"/"Line 2:" prefix; without
            # stripping it a YES parses as NO and the alert is silently dropped.
            verdict = _LABEL_RE.sub("", lines[0]).strip().upper()
            decision = verdict.startswith("YES")
            reason = _LABEL_RE.sub("", lines[1]).strip() if len(lines) > 1 else "(no reason)"
            _note_llm_success()
            return decision, reason
        except Exception as e:
            if "429" in str(e) and attempt < 2:
                wait_time = 60 * (attempt + 1)
                print(f"[WAIT] rate limited, retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            error_reason = f"LLM error: {e}"
            _note_llm_failure(error_reason)
            return False, error_reason

    _note_llm_failure("LLM error: max retries exceeded")
    return False, "LLM error: max retries exceeded"