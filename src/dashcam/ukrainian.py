"""
Ukrainian street names: the street-type words to drop, and the KMU 2010 transliteration.

Street names in OSM for Ukraine are in Ukrainian, sometimes with an English `name:en`. Trip
names use the English name when there is one, and otherwise this transliteration.
"""

# Street-type words dropped from street names before transliteration, in lowercase.
STREET_TYPE_WORDS = frozenset(
    [
        "вулиця",
        "вул.",
        "проспект",
        "просп.",
        "пр.",
        "площа",
        "пл.",
        "провулок",
        "пров.",
        "бульвар",
        "бул.",
        "шосе",
        "дорога",
        "узвіз",
        "набережна",
        "проїзд",
        "тупик",
        "майдан",
        "алея",
        "шлях",
        "тракт",
        "траса",
        "міст",
        "спуск",
        "в'їзд",
        "в’їзд",
        "з'їзд",
        "з’їзд",
        "улица",
    ]
)

# KMU 2010 transliteration of Ukrainian (Cabinet of Ministers resolution No. 55). Letters with
# a separate form at the start of a word map to (initial form, other form).
KMU_2010_LETTERS = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "h",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "є": ("ye", "ie"),
    "ж": "zh",
    "з": "z",
    "и": "y",
    "і": "i",
    "ї": ("yi", "i"),
    "й": ("y", "i"),
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ь": "",
    "ю": ("yu", "iu"),
    "я": ("ya", "ia"),
    # Russian letters, which appear in some older OSM names.
    "ё": ("yo", "io"),
    "ъ": "",
    "ы": "y",
    "э": "e",
}

# Apostrophes are not transliterated.
APOSTROPHES = frozenset("'’ʼ`")


def transliterate(text: str) -> str:
    """Transliterate Ukrainian text to Latin letters using the KMU 2010 system."""
    result = []
    for index, char in enumerate(text):
        if char in APOSTROPHES:
            continue
        lower_char = char.lower()
        mapping = KMU_2010_LETTERS.get(lower_char)
        if mapping is None:
            result.append(char)
            continue
        previous_char = text[index - 1] if index > 0 else ""
        is_word_start = not previous_char.isalpha() and previous_char not in APOSTROPHES
        if isinstance(mapping, tuple):
            mapping = mapping[0] if is_word_start else mapping[1]
        # The combination "зг" is written as "zgh" to tell it apart from "ж".
        if lower_char == "г" and previous_char.lower() == "з":
            mapping = "gh"
        if char.isupper() and mapping:
            next_char = text[index + 1] if index + 1 < len(text) else ""
            is_all_caps_word = next_char.isupper()
            mapping = mapping.upper() if is_all_caps_word else mapping[0].upper() + mapping[1:]
        result.append(mapping)
    return "".join(result)
