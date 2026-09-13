import html
import re
import time
import threading
from google import genai
from config import LLM_API_KEY
from notifier import send_alert

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

KEYWORDS = [
    # Age ranges
    "18-22", "18–22", "18 до 22", "18 до 25", "18-23",
    # Mobilization
    "мобілізац", "мобілізов", "демобілізац",
    # Deferral / exemption
    "відстрочк", "бронювання", "броню", "БЗВП",
    # Border / travel
    "виїзд за кордон", "перетин кордон", "кордон",
    "№57", "постанова 57", "постанову 57", "постанова №57", "постанову №57",
    # Military / draft
    "військовозобов", "призов", "призивн",
    # Institutions
    "ТЦК", "військкомат",
    # Law changes — specific military phrases only (avoid matching "незаконний" etc.)
    "закон про мобілізац", "закон про призов", "закон про відстрочк",
    "законопроект про мобілізац", "законопроект про призов", "законопроект про відстрочк",
    "указ президента",
    # English terms (for forwarded content)
    "mobilization", "conscription", "draft exemption", "border crossing",
]

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


def keyword_match(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in KEYWORDS)


def _note_llm_success() -> None:
    global _consecutive_llm_errors, _llm_error_alerted
    _consecutive_llm_errors = 0
    if _llm_error_alerted:
        _llm_error_alerted = False
        try:
            send_alert("✅ <b>Classifier recovered</b> — posts are being evaluated normally again.")
        except Exception as e:
            print(f"[LLM] failed to send recovery alert: {e}")


def _note_llm_failure(reason: str) -> None:
    global _consecutive_llm_errors, _llm_error_alerted
    _consecutive_llm_errors += 1
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