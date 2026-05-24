from google import genai
from config import LLM_API_KEY

_client = genai.Client(api_key=LLM_API_KEY)

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
    # Random string of keywords to catch more variations
    "атак", "війна", "росія", "агресор", "окупант", "захід", "путін", "російськ", "президент", "економіка", "обстірл", "обстріл", "РФ", "метро", "робот",
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

Respond with exactly two lines answering the main question "Does this post describe a change to exit-abroad rules, mobilization rules, or military obligations for Ukrainian men aged 18–22?" in the following format:
Line 1: YES or NO
Line 2: One sentence explaining why (in English)
"""


def keyword_match(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in KEYWORDS)


def llm_classify(text: str, channel: str) -> tuple[bool, str]:
    prompt = _PROMPT_TEMPLATE.format(channel=channel, text=text[:2000])
    try:
        response = _client.models.generate_content(model="gemini-2.0-flash", contents=prompt)
        lines = response.text.strip().splitlines()
        decision = lines[0].strip().upper().startswith("YES")
        reason = lines[1].strip() if len(lines) > 1 else "(no reason)"
        return decision, reason
    except Exception as e:
        return False, f"LLM error: {e}"
