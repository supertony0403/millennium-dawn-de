"""HOI4 localisation file parsing and writing."""
from __future__ import annotations

import pytest

from mdde import locfile

BOM = b"\xef\xbb\xbf"

SAMPLE = (
    "l_english:\n"
    " # National Spirits\n"
    "\n"
    ' GER_focus:0 "Deutschland zuerst"\n'
    ' plain_key: "No version"\n'
    'noindent_key: "Starts at column 0"\n'
    ' tight:"no space"\n'
    ' quoted:0 "He said \\"hi\\" twice"\n'
    ' ranked: "Rank #1 nation" # trailing comment\n'
    ' dotted.key.sub:1 "Dotted"\n'
    ' bare: "\xa7L"Shoot them again!"\xa7!"\n'
    '#commented_out: "not an entry"\n'
    '   #indented_comment:0 "also not"\n'
)


def entry(lf, key):
    return next(e for e in lf.entries if e.key == key)


def test_header_and_entries_are_recognised():
    lf = locfile.parse_text(SAMPLE)
    assert lf.lang == "english"
    assert lf.header_idx == 0
    assert [e.key for e in lf.entries] == [
        "GER_focus",
        "plain_key",
        "noindent_key",
        "tight",
        "quoted",
        "ranked",
        "dotted.key.sub",
        "bare",
    ]
    assert lf.unparsed == []


@pytest.mark.parametrize(
    "key,ver,val,indent,trail",
    [
        ("GER_focus", "0", "Deutschland zuerst", " ", ""),
        ("plain_key", "", "No version", " ", ""),
        ("noindent_key", "", "Starts at column 0", "", ""),
        ("tight", "", "no space", " ", ""),
        ("quoted", "0", 'He said \\"hi\\" twice', " ", ""),
        ("ranked", "", "Rank #1 nation", " ", " # trailing comment"),
        ("dotted.key.sub", "1", "Dotted", " ", ""),
        ("bare", "", '\xa7L"Shoot them again!"\xa7!', " ", ""),
    ],
)
def test_entry_fields(key, ver, val, indent, trail):
    e = entry(locfile.parse_text(SAMPLE), key)
    assert (e.ver, e.val, e.indent, e.trail) == (ver, val, indent, trail)


def test_render_round_trip_is_identical():
    lf = locfile.parse_text(SAMPLE)
    assert lf.render() == SAMPLE


def test_values_returns_first_occurrence_per_key():
    lf = locfile.parse_text('l_german:\n a: "eins"\n b: "zwei"\n a: "drei"\n')
    assert lf.values() == {"a": "eins", "b": "zwei"}


def test_set_value_keeps_indent_version_and_trail():
    lf = locfile.parse_text(SAMPLE)
    lf.set_value(entry(lf, "ranked"), "Rang 1")
    lf.set_value(entry(lf, "GER_focus"), "Neu")
    rendered = lf.render().split("\n")
    assert ' ranked: "Rang 1" # trailing comment' in rendered
    assert ' GER_focus:0 "Neu"' in rendered
    assert entry(lf, "ranked").val == "Rang 1"


def test_set_language_rewrites_header_only():
    lf = locfile.parse_text(' l_english: # head\n key: "l_english: stays"\n')
    lf.set_language("german")
    assert lf.render() == ' l_german: # head\n key: "l_english: stays"\n'
    assert lf.lang == "german"


def test_append_goes_before_final_newline():
    lf = locfile.parse_text('l_german:\n a: "eins"\n')
    lf.append("b", "zwei")
    assert lf.render() == 'l_german:\n a: "eins"\n b:0 "zwei"\n'
    assert entry(lf, "b").val == "zwei"


def test_append_without_final_newline():
    lf = locfile.parse_text('l_german:\n a: "eins"')
    lf.append("b", "zwei")
    assert lf.render() == 'l_german:\n a: "eins"\n b:0 "zwei"'


def test_unparsable_lines_are_reported_but_kept():
    text = 'l_german:\n a: "eins"\n kaputt ohne anfuehrungszeichen\n'
    lf = locfile.parse_text(text)
    assert lf.unparsed == [2]
    assert lf.render() == text


def test_crlf_is_normalised_to_lf():
    lf = locfile.parse_text('l_german:\r\n a: "eins"\r\n')
    assert entry(lf, "a").val == "eins"
    assert lf.render() == 'l_german:\n a: "eins"\n'


def test_missing_header_gives_no_language():
    lf = locfile.parse_text(' a: "eins"\n')
    assert lf.lang is None
    assert lf.header_idx is None


def test_write_adds_bom_and_parse_strips_it(tmp_path):
    path = tmp_path / "sub" / "x_l_german.yml"
    locfile.write(path, 'l_german:\n a: "\xe4\xf6\xfc"\n')
    raw = path.read_bytes()
    assert raw.startswith(BOM + b"l_german:\n")
    assert raw.count(BOM) == 1
    lf = locfile.parse(path)
    assert lf.lang == "german"
    assert entry(lf, "a").val == "\xe4\xf6\xfc"


def test_parse_reads_file_without_bom(tmp_path):
    path = tmp_path / "x_l_english.yml"
    path.write_bytes(b'l_english:\n a: "x"\n')
    assert locfile.parse(path).values() == {"a": "x"}


@pytest.mark.parametrize(
    "raw,escaped",
    [
        ('Er sagte "Nein"', 'Er sagte \\"Nein\\"'),
        ('schon \\"escaped\\"', 'schon \\"escaped\\"'),
        ("„Deutsch“", "„Deutsch“"),
        ("Zeile\\nZwei", "Zeile\\nZwei"),
        ('"', '\\"'),
    ],
)
def test_escape_value(raw, escaped):
    assert locfile.escape_value(raw) == escaped


@pytest.mark.parametrize("bad", ["Zeile\nZwei", "Zeile\rZwei", "endet mit \\"])
def test_escape_value_rejects_breaking_input(bad):
    with pytest.raises(ValueError):
        locfile.escape_value(bad)


# -- review finding (small 5): trailing comment that contains quotes ----------------------
@pytest.mark.parametrize(
    "line,val,trail",
    [
        (' KEY:0 "Wert" # alt: "Old value"', "Wert", ' # alt: "Old value"'),
        (' k: "a "b" c" # "x"', 'a "b" c', ' # "x"'),
        (' k: "Er sagte \\"hi\\"" # "c"', 'Er sagte \\"hi\\"', ' # "c"'),
        (' k: "Pfad C:\\\\" # "c"', "Pfad C:\\\\", ' # "c"'),
        # bare quotes without a comment keep the greedy reading (real MD line)
        (' loading_drill_master_desc:0 "§L"Shoot them again, they\'re not dead yet!"§!"',
         '§L"Shoot them again, they\'re not dead yet!"§!', ""),
        (' ranked: "Rank #1 nation" # trailing comment', "Rank #1 nation", " # trailing comment"),
        (' junk: "value" trailing junk', "value", " trailing junk"),
    ],
)
def test_value_ends_at_the_quote_before_a_comment(line, val, trail):
    lf = locfile.parse_text("l_german:\n" + line + "\n")
    (e,) = lf.entries
    assert (e.val, e.trail) == (val, trail)
    assert e.line() == line
