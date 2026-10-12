"""Validation of translated values against their English source."""
from __future__ import annotations

import pytest

from mdde.validate import check, tokens


def test_token_multiset():
    assert tokens("§Y$A$§! [ROOT.GetName] £pol_power \\n @GER $A$") == {
        "§Y": 1,
        "$A$": 2,
        "§!": 1,
        "[ROOT.GetName]": 1,
        "£pol_power": 1,
        "\\n": 1,
        "@GER": 1,
    }


@pytest.mark.parametrize(
    "en,de",
    [
        ("§YGerman Unity§!", "§YDeutsche Einheit§!"),
        ("$COUNT$ divisions in [ROOT.GetName]", "In [ROOT.GetName] stehen $COUNT$ Divisionen"),
        ("Gain £pol_power\\nNow", "Erhalte £pol_power\\nJetzt"),
        ("The Iron Lady", "The Iron Lady"),
        (
            "Agreement with the Bank of Canada on interest rates",
            "Abkommen mit der Bank of Canada über Zinssätze",
        ),
        (
            "The government wants to reform the law and the order today",
            "Die Regierung will das Gesetz und die Ordnung heute reformieren",
        ),
        ('He said "No"', "Er sagte „Nein“"),
        ('He said "No"', 'Er sagte "Nein"'),
        ("Pay $21 billion to France", "Zahle 21 Mrd. US-Dollar an Frankreich"),
        ("Pay $21 billion to France", "Zahle 21 Mrd. $ an Frankreich"),
    ],
)
def test_valid_translations_pass(en, de):
    assert check(en, de) is None


@pytest.mark.parametrize(
    "en,de,reason",
    [
        ("German Unity", "", "empty"),
        ("German Unity", "   ", "empty"),
        ("German Unity", None, "empty"),
        ("German Unity", 42, "empty"),
        ("First line. Second line.", "Erste Zeile.\nZweite Zeile.", "newline"),
        ("First line. Second line.", "Erste Zeile.\r Zweite.", "newline"),
        ("Ends here", "Endet hier\\", "backslash"),
        ("§YGerman Unity§!", "Deutsche Einheit", "tokens"),
        ("Line one", "Zeile eins\\nZeile zwei", "tokens"),
        ("$COUNT$ divisions", "$ANZAHL$ Divisionen", "tokens"),
        ("Gain £pol_power", "Erhalte £pol_power £pol_power", "tokens"),
        ("Gain 5 stability", "Erhalte 5 § Stabilität", "tokens"),
        ("Gain 5 stability", "Erhalte 5 [Stabilität", "tokens"),
        (
            "We will strengthen the bonds of our nation with the allies",
            "We will strengthen the bonds of our nation with the allies",
            "english",
        ),
        (
            "We will strengthen the bonds of our nation with the allies",
            "Wir stärken the bonds of our nation mit den Verbündeten",
            "english",
        ),
    ],
)
def test_invalid_translations_are_rejected(en, de, reason):
    assert check(en, de) == reason
