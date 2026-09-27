import pytest

from dashcam import ukrainian


@pytest.mark.parametrize(
    ("ukrainian_text", "latin_text"),
    [
        ("Згорани", "Zghorany"),
        ("Знам’янка", "Znamianka"),
        ("Короп’є", "Koropie"),
        ("Єнакієве", "Yenakiieve"),
        ("Їжакевич", "Yizhakevych"),
        ("Мар'їне", "Marine"),
        ("Юрій", "Yurii"),
        ("Щербухи", "Shcherbukhy"),
        ("Ґорґани", "Gorgany"),
        ("Івана-Теодозія Куровця", "Ivana-Teodoziia Kurovtsia"),
    ],
)
def test_transliterate(ukrainian_text, latin_text):
    assert ukrainian.transliterate(ukrainian_text) == latin_text


def test_transliterate_keeps_capitals_of_all_caps_words():
    assert ukrainian.transliterate("ЩЕ Ж") == "SHCHE Zh"
