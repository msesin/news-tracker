import html
import re
import time
import threading
from datetime import datetime, timezone
from google import genai
from config import LLM_API_KEY
from notifier import send_alert, send_text

# Tried in order until one answers. A 503 "high demand" is capacity on one
# model, not the API as a whole: on 2026-09-24 gemini-3.1-flash-lite returned
# 503 for about five hours straight, and a same-day test found other models
# answering while it was busy. Free-tier rate limits are also per model, so a
# 429 on one model is no reason to wait before asking the next.
#
# The order spreads the chain across generations and tiers, so the models are
# unlikely to be saturated at the same moment: the primary, a Flash-Lite from
# another generation, then full Flash models from older to newer — demand
# piles onto the newest release, so it is the last resort. All are stable
# endpoints; previews usually need billing, and the 2.5 models return 404 for
# a key that hadn't used them before Google restricted access. Every model
# here was checked against the real prompt and answered in the two-line
# YES/NO format.
_MODELS = (
    "gemini-3.1-flash-lite",   # primary: cheapest and fastest
    "gemini-3.5-flash-lite",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
)
_client = genai.Client(api_key=LLM_API_KEY)
print(f"[LLM] models={' → '.join(_MODELS)}")

# Rate limiter: free tier = 15 RPM; 5 s gap → 12 RPM to avoid edge-of-window 429s
_lock = threading.Lock()
_last_call_time = 0.0
_MIN_INTERVAL = 5.0
_LABEL_RE = re.compile(r"^\s*line\s*\d+\s*:\s*", re.I)

# Transient provider errors survive up to 2 in-call retries with backoff before
# giving up; permanent ones (bad key, malformed request) don't retry at all,
# since retrying them only burns quota. Requiring 2 separate failed calls before
# alerting avoids a false alarm from one blip, while still catching a real
# outage fast.
#
# 503 UNAVAILABLE is the provider saying "too much demand right now, try again"
# — its own message calls the spike temporary. It used to fall through to the
# no-retry branch alongside genuinely permanent failures, so a 503 burned the
# post immediately: one call, logged NO unreviewed, no second attempt. It is
# retried on its own schedule, separate from 429's.
#
# The waits are deliberately long. A demand spike that clears in five seconds
# would not have produced an alert in the first place; the outages worth
# retrying through last minutes, and three attempts crammed into fifteen
# seconds just fail three times and burn the post. 30s then 60s buys a post
# ninety seconds of outage tolerance instead of fifteen. Anything longer than
# that is the parking table's job, not the retry loop's.
_RETRYABLE = ("429", "503", "UNAVAILABLE", "500", "INTERNAL", "502", "504", "DEADLINE_EXCEEDED")


def _is_retryable(err: str) -> bool:
    return any(code in err for code in _RETRYABLE)


def _backoff_seconds(err: str, attempt: int) -> int:
    """429 waits out a rate-limit window; everything else rides out a spike."""
    return 60 * (attempt + 1) if "429" in err else 30 * (attempt + 1)

_LLM_ERROR_ALERT_THRESHOLD = 2
_consecutive_llm_errors = 0
_llm_error_alerted = False

# The process itself can be perfectly healthy — connected to Telegram, pinging
# healthchecks.io on schedule — while the classifier is the thing that's broken
# (quota exhausted, provider outage). healthchecks can't see that: it only
# watches for a heartbeat, and this failure doesn't stop the heartbeat. So this
# is the one channel-facing outage notice that has to come from inside the
# process rather than from an external ping. It uses the same 15-minute bar as
# the dead man's switch, measured from the first failure. The bar is checked
# on every failed classification and, once an outage has started, every
# minute by the probe below — so it fires on time even if no post arrives.
_LLM_CHANNEL_ALERT_AFTER = 15 * 60
_llm_outage_since: datetime | None = None
_llm_channel_alerted = False

# Recovery used to be discovered only by handing the classifier a real post:
# _note_llm_success() runs inside llm_classify(), and llm_classify() only runs
# for a post that already cleared keyword_match(). On a quiet stretch the API
# could come back within a minute and nobody would be told for hours, because
# no qualifying post arrived to prove it. The same coupling delayed the
# channel-facing notice in the other direction.
#
# So while an outage is open, a background thread probes the API on its own.
# The cost objection that ruled this out before only applies while healthy —
# once we know we are down, real posts are not consuming quota anyway, and one
# short call every few minutes is cheap next to silently logging posts NO.
_PROBE_INTERVAL = 60
_PROBE_TEXT = "ping"
_probe_thread: threading.Thread | None = None
_probe_stop = threading.Event()


def classifier_healthy() -> bool:
    """True when the last call we made was answered. The drain uses this to
    avoid replaying a backlog into an API that is still down: a failure then
    says nothing about the post, only about the outage, and counting it as a
    strike against the post would throw away exactly the posts parking exists
    to protect."""
    return _consecutive_llm_errors == 0


def _probe_once() -> bool:
    """One bare request down the model chain. True if any model answers —
    that is what "the classifier is back" means once there is a fallback."""
    try:
        _generate(_PROBE_TEXT)
        return True
    except Exception as e:
        print(f"[LLM] probe still failing: {str(e)[:120]}")
        return False


def _probe_loop() -> None:
    while not _probe_stop.wait(_PROBE_INTERVAL):
        # The channel clock has to keep running here too. Previously it was
        # only evaluated on a failed classification, so a silent outage could
        # outlive the 15-minute bar without the channel ever being told.
        _maybe_alert_channel_outage()
        if _probe_once():
            print("[LLM] probe succeeded — classifier is back")
            _note_llm_success()
            return


def _start_probe() -> None:
    global _probe_thread
    if _probe_thread is not None and _probe_thread.is_alive():
        return
    _probe_stop.clear()
    _probe_thread = threading.Thread(target=_probe_loop, name="llm-probe", daemon=True)
    _probe_thread.start()


def _maybe_alert_channel_outage() -> None:
    """Tell the channel once the outage passes the 15-minute bar. Split out of
    _note_llm_failure() so the probe thread can apply the same rule without a
    post having to arrive."""
    global _llm_channel_alerted
    if _llm_channel_alerted or _llm_outage_since is None:
        return
    if (datetime.now(timezone.utc) - _llm_outage_since).total_seconds() < _LLM_CHANNEL_ALERT_AFTER:
        return
    _llm_channel_alerted = True
    try:
        send_text(CHANNEL_DOWN_MESSAGE)
    except Exception as e:
        print(f"[LLM] failed to send channel outage notice: {e}")

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
    _probe_stop.set()          # whichever path got here first, the probe is done
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
                f"Matching posts are being parked and will be classified "
                f"automatically once it recovers — expect them late, not lost."
            )
        except Exception as e:
            print(f"[LLM] failed to send failure alert: {e}")

    _maybe_alert_channel_outage()

    # From here the probe watches for recovery, so it no longer takes a
    # qualifying post to notice the API is back — or to reach the 15-minute
    # channel bar while nothing is arriving.
    _start_probe()


def _generate(prompt: str) -> tuple[str, str]:
    """Ask each model in _MODELS in turn, with no wait in between, and return
    (response text, model) from the first that answers.

    Any error moves on to the next model — busy, rate-limited, or even gone
    (a model Google later shuts down returns 404 and is simply skipped). If
    every model fails, the error raised is a retryable one when any model's
    error was, so the caller backs off and tries the whole chain again rather
    than giving up because the *last* model in line happened to fail
    differently."""
    errors = []
    for model in _MODELS:
        try:
            response = _client.models.generate_content(model=model, contents=prompt)
            if not (response.text or "").strip():
                raise ValueError("empty response")
            if errors:
                print(f"[LLM] answered by fallback {model} after {len(errors)} busy model(s)")
            return response.text, model
        except Exception as e:
            errors.append(str(e))
            print(f"[LLM] {model} failed: {str(e)[:90]}")
    representative = next((e for e in errors if _is_retryable(e)), errors[-1])
    raise RuntimeError(f"all {len(_MODELS)} models failed; e.g. {representative}")


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
            answer, model = _generate(prompt)
            lines = answer.strip().splitlines()
            # The model intermittently echoes a "Line 1:"/"Line 2:" prefix; without
            # stripping it a YES parses as NO and the alert is silently dropped.
            verdict = _LABEL_RE.sub("", lines[0]).strip().upper()
            decision = verdict.startswith("YES")
            reason = _LABEL_RE.sub("", lines[1]).strip() if len(lines) > 1 else "(no reason)"
            if model != _MODELS[0]:
                reason = f"{reason} (via {model})"
            _note_llm_success()
            return decision, reason
        except Exception as e:
            err = str(e)
            if _is_retryable(err) and attempt < 2:
                wait_time = _backoff_seconds(err, attempt)
                print(f"[WAIT] every model busy, retrying the chain in {wait_time}s...")
                time.sleep(wait_time)
                continue
            error_reason = f"LLM error: {e}"
            _note_llm_failure(error_reason)
            return False, error_reason

    _note_llm_failure("LLM error: max retries exceeded")
    return False, "LLM error: max retries exceeded"