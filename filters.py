KEYWORDS = [
    # Age ranges
    "18-22", "18–22", "18 до 22", "18 до 25",
    # Mobilization
    "мобілізац", "мобілізов", "демобілізац",
    # Deferral / exemption
    "відстрочк", "бронювання", "броню",
    # Border / travel
    "виїзд за кордон", "перетин кордон", "кордон",
    # Military / draft
    "військовозобов", "призов", "призивн",
    # Institutions
    "ТЦК", "військкомат",
    # Law changes
    "закон", "законопроект", "указ президента",
    # English terms (for forwarded content)
    "mobilization", "conscription", "draft exemption", "border crossing",
]


def keyword_match(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in KEYWORDS)
