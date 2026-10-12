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
            "unchanged",
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


# -- review findings W2: gaps that used to pass -----------------------------------
LONG_EN = (
    "The Bundeswehr has suffered from chronic underfunding for decades. Equipment is "
    "outdated, readiness is low and morale is at a historic low point."
)


@pytest.mark.parametrize(
    "en,de,reason",
    [
        (LONG_EN, "Die Bundeswehr", "length"),
        ("Short source text of about fifty characters total", "Ein " * 60, "length"),
        ("Reform the army", ".", "notext"),
        ("Reform the army", "§Y§!", "tokens"),
        ("Reform the army", "Hier ist die Übersetzung: Die Armee reformieren", "chatter"),
        ("Reform the army", "Here is the translation: Die Armee reformieren", "chatter"),
        ("Reform the army", "Übersetzung: Die Armee reformieren", "chatter"),
        ("Reform the army", "Translation: Die Armee reformieren", "chatter"),
        ("Reform the army", "Die Armee reformieren (Übersetzung: Reform the army)", "chatter"),
        ("Reform the army", "„Die Armee reformieren“", "chatter"),
        ("Reform the army", '"Die Armee reformieren"', "chatter"),
        ("Reform the army", "Reform the army", "unchanged"),
        ("Reform the army", "Reform the Army ", "unchanged"),
        (
            "We must strengthen our alliance with France.",
            "We must strengthen our alliance with France.",
            "unchanged",
        ),
        ("§YWar§! is coming", "§!Krieg§Y kommt", "order"),
        ('Follow \\"new trends\\" now', 'Folge \\„neuen Trends\\“ jetzt', "backslash"),
        ("Line one\\nLine two", "Zeile eins\\\\nZeile zwei", "backslash"),
        ("Ministry of the Interior", "Ministerium of the Interior", "english"),
        ("Focus Tree of Crimea", "Tree of Focuses of Crimea", "english"),
    ],
)
def test_review_gaps_are_rejected(en, de, reason):
    assert check(en, de) == reason


@pytest.mark.parametrize(
    "en,de",
    [
        # proper names must keep passing (no false "english")
        ("Agreement with the Bank of Canada", "Abkommen mit der Bank of Canada"),
        ("Saudi Aramco shares fall", "Aktien von Saudi Aramco fallen"),
        ("Bank of Canada reforms", "Bank of Canada Reformen"),
        ("Exxon and Mobil merge into ExxonMobil", "Exxon und Mobil fusionieren zu ExxonMobil"),
        # very short but valid German
        ("Yes", "Ja"),
        ("No", "Nein"),
        # German verb-final word order moves coloured spans (real MD translations)
        (
            "Our Forces §HPopularity§! will §GIncrease§! by §H1%§!",
            "Die §HBeliebtheit§! unserer Streitkräfte wird um §H1%§! §Gsteigen§!",
        ),
        ("Will be §Rblocked§! from any further attempts at §YIntegration§!",
         "Wird von allen weiteren Versuchen der §YIntegration§! §Rausgeschlossen§!"),
        # "Hier ist" without any chatter about a translation is plain German
        ("A reform is needed here, whoever we appoint", "Hier ist eine Reform erforderlich, wen auch immer wir ernennen"),
        # quotes that belong to the source are fine
        ('\\"Freedom\\" for everyone', '\\"Freiheit\\" für alle'),
        ('\\"Freedom\\"', "„Freiheit“"),
        # escaped quotes and line breaks are allowed backslashes
        ('He said \\"No\\"\\nThen he left', 'Er sagte \\"Nein\\"\\nDann ging er'),
        # length ratio only applies to sources with >= 40 characters (without tokens)
        ("Form a coalition government", "Eine Koalitionsregierung bilden"),
        ("§Y[ROOT.GetName]§! $VALUE$ [FROM.GetNameWithFlag] gains", "[ROOT.GetName] §Y§! $VALUE$ [FROM.GetNameWithFlag] gewinnt"),
    ],
)
def test_valid_translations_still_pass(en, de):
    assert check(en, de) is None


@pytest.mark.parametrize("en,de", [("ExxonMobil", "ExxonMobil"), ("Bank of Canada", "Bank of Canada"),
                                   ("Saudi Aramco", "Saudi Aramco")])
def test_unchanged_proper_names_are_unchanged_not_english(en, de):
    assert check(en, de) == "unchanged"


def test_text_without_letters_may_stay_identical():
    assert check("§Y12%§!", "§Y12%§!") is None


def test_length_ratio_ignores_tokens():
    en = "§Y[ROOT.GetNameWithFlag]§! [FROM.GetNameWithFlag] [ROOT.GetAdjective] $VALUE$ gains"
    assert check(en, "§Y[ROOT.GetNameWithFlag]§! [FROM.GetNameWithFlag] [ROOT.GetAdjective] $VALUE$ gewinnt") is None


@pytest.mark.parametrize(
    "en,de",
    [
        # acronyms that look like English stop words ("CAN", "IT", "AT", "A")
        (
            "The Leopard 2A4M CAN is the upgraded Canadian version of the Leopard 2A4. The CAN tank is a re-conditioned Leopard",
            "Der Leopard 2A4M CAN ist die aufgerüstete kanadische Version des Leopard 2A4. Der CAN-Panzer ist ein umgebauter Leopard",
        ),
        (
            "IT security and AT weapons for the army of the future",
            "IT-Sicherheit und AT-Waffen für die Armee der Zukunft, Typ A",
        ),
        # short title without German stop words: only skipping acronyms keeps it valid
        ("Leopard 2A4M CAN Main Battle Tank", "Kampfpanzer Leopard 2A4M CAN"),
        ("IT and AT Systems", "IT AT Systeme"),
        # German text that keeps an English designer hint (real Millennium Dawn value)
        (
            "The addition of integrated health monitoring systems has allowed predictive maintenance."
            "\\n\\n§GDesigner Hint: Piston engines are the budget alternative to Turbine engines§!",
            "Die Einführung integrierter Überwachungssysteme erhöht die Zuverlässigkeit und vereinfacht die Wartung. "
            "Fortschritte in Design und Metallurgie tragen zur Leistung bei."
            "\\n\\n§GDesigner Hint: Piston engines are the budget alternative to Turbine engines§!",
        ),
    ],
)
def test_german_with_acronyms_or_quotes_is_not_english(en, de):
    assert check(en, de) is None


# -- review 2, W-3: doubly escaped backslashes are repaired before the check -------------
from mdde.validate import repair_escapes  # noqa: E402


@pytest.mark.parametrize(
    "en,de,fixed",
    [
        ("Line one\\nLine two", "Zeile eins\\\\nZeile zwei", "Zeile eins\\nZeile zwei"),
        ('He said \\"no\\"', 'Er sagte \\\\"nein\\\\"', 'Er sagte \\"nein\\"'),
        ("§YA§!\\n\\nB", "§YA§!\\\\n\\\\nB", "§YA§!\\n\\nB"),
    ],
)
def test_doubly_escaped_line_breaks_and_quotes_are_repaired(en, de, fixed):
    assert check(en, de) == "backslash"
    assert repair_escapes(en, de) == fixed
    assert check(en, repair_escapes(en, de)) is None


@pytest.mark.parametrize(
    "en,de",
    [
        ("Path C:\\\\new", "Pfad C:\\\\neu"),  # the source itself has a double backslash
        ("Line one\\nLine two", "Zeile eins\\nZeile zwei"),  # nothing to repair
        ("Ends here", "Endet hier\\"),  # a lone backslash is no double escape
    ],
)
def test_repair_leaves_other_texts_alone(en, de):
    assert repair_escapes(en, de) == de


def test_repair_ignores_non_strings():
    assert repair_escapes("x", None) is None


# -- review 2, W-4: unchanged answers that obviously still need German -------------------
from mdde.validate import looks_translatable  # noqa: E402


@pytest.mark.parametrize(
    "en",
    [
        "§YToggle Cheat Decisions§!",
        "§REnd§! USAID",
        "Islamic Emirate of Arabia",
        "the Islamic Emirate of Tajikistan",
        "Army Leader Cost: $RIGHT|-=%1$",
        "§CAntarctic Treaty Consultative Parties§!",
        "Kharkiv People's Republic",
        "§GActive§!",
        "§YPrerequisites:§!\\n[!eu_parliament_president_back_click_enabled]",
        "We must reform the economy before the election.",
        "Stability Bonus",
        "Level [?ROOT.MD_ftc_academy_level|0]/3",
        "§GHIRED§!",
        "§RMISSING§!",
    ],
)
def test_unchanged_but_obviously_translatable(en):
    assert looks_translatable(en)


@pytest.mark.parametrize(
    "en",
    [
        "Opposition",
        "Initiative: $RIGHT|+=%1$",
        "ExxonMobil",
        "Maghreb",
        "Fabiola Gianotti",
        "Wörth am Rhein",
        "Osama bin Laden",
        "Ansar al-Sharia",
        "Manuel Pinto da Costa.",
        "§R[?temp_apc_equipment_cost|3R]b/CPW§!",
        "[1182.owner.GetAdjective] Dollar",
        "NATO",
        "§Y$VALUE$§!",
    ],
)
def test_legitimately_unchanged_texts_are_not_translatable(en):
    assert not looks_translatable(en)
