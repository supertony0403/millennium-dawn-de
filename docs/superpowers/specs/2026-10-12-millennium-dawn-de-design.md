# Millennium Dawn – Deutsche Übersetzung (Submod) — Design

Stand: 12.10.2026 · Mod-Version Millennium Dawn 2.0.2 · HOI4 1.19.3 (nativ, Linux)

## Ziel

Der Spieler will **Deutschland** in Millennium Dawn komplett auf Deutsch spielen:
Deutschland-Fokusbaum, Events, Ergebnisse, Entscheidungen, Nationalgeister,
Technologien, UI. Fokusbäume anderer Nationen sind ausdrücklich egal.

Millennium Dawn bringt einen `localisation/german`-Ordner mit, aber ~83 % der
Werte sind englische Kopien. Dieser Submod füllt die Lücken per
maschineller Übersetzung über **kostenlose OpenRouter-Modelle**; Claude
orchestriert und prüft Stichproben, übersetzt aber nicht selbst (Claude-Limit
bleibt frei, 0 € Kosten).

## Messlauf (12.10.2026, `MD_focus_CAN`, 941 Texte)

- `nvidia/nemotron-3-super-120b-a12b:free`: 939/941 gültig, 12 Anfragen (~80 Texte/Anfrage),
  Ø 138 s/Anfrage, 16,5 min mit 3 parallelen Anfragen. Qualität: Beschreibungen gut,
  kurze Event-Optionen gelegentlich holprig.
- `google/gemma-4-*-it:free`: dauerhaft HTTP 429 „rate-limited upstream“ (geteilter Google-Pool).
- `thinkingmachines/inkling:free`: HTTP 403 (nur für bestimmte Apps).
- Konto: 1.000 Gratis-Anfragen/Tag (account-weit, geteilt mit dem Discord-Bot „Fred“).

## Umfang

**Scope `germany` (Standard), ~37.300 Texte, ~3,5 Mio. Zeichen, ~480 Anfragen:**

- alle Lokalisierungsdateien **außer**
  - `MD_focus_<TAG>*` mit `<TAG>` ≠ `GER` (TAG = echtes Länderkürzel aus `common/country_tags/*.txt`)
  - reinen Namenslisten: `victory_points`, `state_names`, `MD_terrain_cities`,
    `strategic_region_names`, `supply_area_names`
- innerhalb der übrigen Dateien werden Schlüssel übersprungen, die mit einem
  **fremden** Länderkürzel beginnen (`RUS_…`, `USA.…`, `FRA` als ganzer Schlüssel);
  Ausnahme: Dateien `countries`, `countries_cosmetic` (Ländernamen werden übersetzt).

**Scope `full`:** alles außer den Namenslisten (~180.000 Texte) – optional, später.

**„Offener“ Text** = Schlüssel aus der englischen Datei, dessen Wert in Millennium Dawns
deutscher Lokalisierung fehlt oder identisch mit dem englischen ist, und der nach Entfernen
aller Formatierungs-Tokens noch ≥3 aufeinanderfolgende Buchstaben enthält.
Vorhandene deutsche Übersetzungen von Millennium Dawn bleiben unangetastet.

## Ausgabe: wie der Submod überschreibt

- Submod-Ordner = dieses Repo:
  `~/.local/share/Paradox Interactive/Hearts of Iron IV/mod/millennium_dawn_de`
- `descriptor.mod` mit `dependencies = { "Millennium Dawn: A Modern Day Mod" }` → lädt nach MD.
- Für **jede** deutsche Datei von Millennium Dawn (`localisation/german/**`, inkl. `replace/`)
  schreibt der Submod eine Datei **unter exakt demselben relativen Pfad**. Gleicher Pfad im
  später geladenen Mod ersetzt die Datei vollständig → keine doppelten Schlüssel.
- Inhalt = MDs deutsche Datei Zeile für Zeile (Kommentare, Reihenfolge, Zusatzschlüssel
  bleiben), nur Werte offener Texte werden durch die gecachte Übersetzung ersetzt.
- Englische Dateien ohne deutsches Gegenstück → neue Datei `localisation/german/<rel>/<base>_l_german.yml`
  aus der englischen Struktur.
- Englische Schlüssel, die in keiner deutschen Datei vorkommen → ans Ende der passenden Datei.
- Dateien: UTF-8 **mit BOM**, Kopfzeile `l_german:`, Werte mit `\"` für Anführungszeichen.

## Komponenten (Python-Paket `tools/mdde`, nur Standardbibliothek + pytest)

| Modul | Aufgabe |
|---|---|
| `locfile` | HOI4-Loc lesen/schreiben (BOM, `key:0 "wert"`, Kommentare, Escapes) |
| `scope` | Länderkürzel laden, offene Texte je Scope bestimmen |
| `cache` | `cache/translations.jsonl` (append-only): `{key, src_sha1, de, model, ts}`; Treffer nur bei gleichem Hash des aktuellen englischen Textes |
| `validate` | Token-Multimenge gleich (§X…§!, £icon, $VAR$, [Scope.Func], \n, @TAG), nicht leer, keine echten Zeilenumbrüche, Englisch-Rest-Heuristik |
| `openrouter` | Chat-Completions-Client, Modellkette, Fehlerbehandlung, Tageskontingent |
| `translate` | Pakete bilden (≤9.000 Zeichen, ≤100 Texte), parallel (Standard 3), zweite Runde für Ausfälle (Pakete à 10) |
| `build` | Ausgabedateien erzeugen (siehe oben) |
| `install` | `.mod`-Zeiger, Eintrag im nativen Launcher (`launcher-v2.sqlite`, nach MD im aktiven Playset), `dlc_load.json`; DB-Backup vorher, idempotent |
| `__main__` | CLI: `status`, `translate`, `build`, `install`, `check` |

Begriffsliste `tools/glossary.json` (offizielle deutsche HOI4-Begriffe) wird in den Systemprompt eingefügt.

## Fehlerbehandlung

- 429 → sofort nächstes Modell; alle Modelle 429 → warten (60 s, steigend bis 5 min).
- 403/404 → Modell für den Rest des Laufs aus der Kette nehmen.
- Timeout/5xx/kaputtes JSON → nächstes Modell, max. 3 Runden pro Paket.
- Ungültige Einzeltexte → zweite Runde in kleinen Paketen; danach `cache/rejects.jsonl`, Fallback = englischer Text.
- Tageskontingent: vor dem Start und alle 50 Anfragen `GET /api/v1/key` →
  `free_model_daily_requests.remaining`; Stopp bei ≤ Reserve (Standard 100, für Fred).
  `--wait-for-reset` schläft bis 00:00 UTC und macht weiter.
- Abbruch jederzeit möglich: jeder fertige Text liegt sofort im Cache; Neustart überspringt ihn.

## Sicherheit

- API-Schlüssel aus `OPENROUTER_API_KEY` oder Datei in `MDDE_KEY_FILE`
  (Standard `~/Documents/Programmierung/Backup/Discord-Bot/.env`), nie geloggt, nie im Repo.
- Mod-Texte sind Fremdtext; sie gehen nur als Daten an eine Chat-API ohne Werkzeuge.

## Updates von Millennium Dawn

`status` erkennt neue/geänderte englische Texte über den Hash. `translate` übersetzt nur diese,
`build` erzeugt die Dateien neu. Ohne Neu-Build nach einem MD-Update können neue Schlüssel
fehlen → `check` warnt, wenn MD-Version/Dateistand nicht zum letzten Build passt.

## Erfolgskriterien

1. `check`: alle Ausgabedateien parsen, BOM + `l_german:`, keine kaputten Tokens.
2. Scope `germany`: ≥98 % der offenen Texte übersetzt, Rest in `rejects.jsonl`.
3. Submod im Launcher aktiv, hinter Millennium Dawn.
4. Im Spiel: Deutschland-Fokusbaum, ein allgemeines Event und das Technologiemenü auf Deutsch;
   `error.log` ohne neue Lokalisierungsfehler gegenüber dem Stand ohne Submod.
