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


def keyword_match(text: str) -> bool:
    lower = text.lower()
    return any(kw.lower() in lower for kw in KEYWORDS)
