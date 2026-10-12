# Millennium Dawn – Deutsche Übersetzung (Submod)

Übersetzungs-Submod für die Hearts-of-Iron-IV-Mod **Millennium Dawn** (2.0.2, HOI4 1.19).
Millennium Dawn bringt einen `localisation/german`-Ordner mit, doch rund 83 % der Werte
sind englische Kopien. Dieser Submod füllt die Lücken per maschineller Übersetzung über
**kostenlose OpenRouter-Modelle**; vorhandene deutsche Texte von Millennium Dawn bleiben
unangetastet.

Design: `docs/superpowers/specs/2026-10-12-millennium-dawn-de-design.md` ·
Plan: `docs/superpowers/plans/2026-10-12-millennium-dawn-de-plan.md`

## Umfang

| Scope | Inhalt | offene Texte (MD 2.0.2) |
|---|---|---|
| `germany` (Standard) | alles außer fremden Fokusbäumen (`MD_focus_<TAG>`), Namenslisten und Schlüsseln mit fremdem Länderkürzel (`RUS_…`, `USA.…`) – Ausnahme Ländernamen-Dateien (`countries`, `MD_countries_cosmetic`, `replaced_from_vanilla_countries_cosmetic`) | 39.411 (32.689 verschiedene Texte, ≈ 440 Anfragen) |
| `full` | alles außer Namenslisten (`victory_points`, `state_names`, `MD_terrain_cities`, `strategic_region_names`, `supply_area_names`) | 175.203 (≈ 2.080 Anfragen) |

Gleiche englische Texte unter verschiedenen Schlüsseln werden nur einmal angefragt.

„Offen“ ist ein englischer Text, dessen deutscher Wert bei Millennium Dawn fehlt oder
identisch mit dem englischen ist und der ohne Formatierungs-Tokens noch mindestens drei
aufeinanderfolgende Buchstaben enthält.

## Aufruf

Alle Befehle laufen aus dem Ordner `tools/` (nur Python-Standardbibliothek, Python ≥ 3.11):

```bash
cd tools
python3 -m mdde status --scope germany   # offene Texte, Cache-Stand, Tageskontingent
python3 -m mdde status --scope full --no-quota
python3 -m mdde translate                # übersetzt offene Texte (Scope germany)
python3 -m mdde translate --max-requests 3          # Probelauf
python3 -m mdde translate --wait-for-reset          # bei leerem Kontingent bis 00:00 UTC warten
python3 -m mdde build                    # erzeugt localisation/german/** + build_info.json
python3 -m mdde check                    # prüft die Ausgabe (Exit 0 ok, 1 Fehler, 2 veraltet)
python3 -m mdde install --yes            # trägt den Submod im Launcher ein
```

Lange Übersetzungsläufe vor dem Standby schützen:
`systemd-inhibit --what=sleep:idle python3 -m mdde translate --wait-for-reset`

### Ablauf

1. `status` – zeigt, wie viel zu tun ist (und wie viele Anfragen das ungefähr kostet).
2. `translate` – jedes gültige Ergebnis landet sofort in `cache/translations.jsonl`;
   Abbruch jederzeit möglich, ein Neustart überspringt Fertiges. Ungültige Texte bekommen
   eine zweite Runde in kleinen Paketen, danach landen sie in `cache/rejects.jsonl`
   (im Spiel bleibt dann der englische Text stehen).
3. `build` – spiegelt **jede** deutsche Datei von Millennium Dawn unter exakt demselben
   relativen Pfad (inkl. `replace/`) und ersetzt nur offene Werte durch die Übersetzung.
   Englische Dateien ohne deutsches Gegenstück werden als neue `_l_german.yml` angelegt,
   fehlende Schlüssel ans Dateiende gehängt.
4. `check` – parst alle Ausgabedateien (BOM, Kopfzeile `l_german:`, Token-Gleichheit) und
   warnt, wenn Millennium Dawn seit dem letzten Build aktualisiert wurde.
5. `install` – **Paradox-Launcher vorher schließen!** Schreibt den Zeiger
   `mod/millennium_dawn_de.mod`, legt ein Backup von `launcher-v2.sqlite` an, trägt den
   Submod im aktiven Playset direkt **hinter** Millennium Dawn ein und ergänzt
   `dlc_load.json`. Mehrfach ausführbar.

Nach einem Update von Millennium Dawn: `status` → `translate` → `build` → `check`.

Exit-Codes: `check` 0 = in Ordnung, 1 = Fehler in der Ausgabe, 2 = Build veraltet
(Millennium Dawn hat sich seit dem letzten `build` geändert). `translate` endet mit 1 nur bei
Schlüssel-/Modellfehlern; ein Stopp wegen Kontingent oder `--max-requests` ist kein Fehler.

## Dateien

| Pfad | Inhalt |
|---|---|
| `tools/mdde/` | Python-Paket (`config`, `locfile`, `scope`, `cache`, `validate`, `openrouter`, `translate`, `build`, `install`, `cli`) |
| `cache/translations.jsonl` | Übersetzungs-Cache (append-only, Treffer nur bei gleichem SHA-1 des englischen Textes) |
| `cache/rejects.jsonl` | Texte, die auch nach der zweiten Runde ungültig waren |
| `logs/translate.log`, `logs/last_run.json` | Fortschritt und Abschlussbericht des letzten Laufs (nicht im Repo) |
| `localisation/german/` | generierte Ausgabe (nicht von Hand bearbeiten – `build` überschreibt sie) |
| `build_info.json` | MD-Version, Fingerabdruck der MD-Lokalisierung, Zahlen des letzten Builds |

## Konfiguration (Umgebungsvariablen)

| Variable | Standard |
|---|---|
| `OPENROUTER_API_KEY` | sonst aus der Datei in `MDDE_KEY_FILE` |
| `MDDE_KEY_FILE` | `~/Documents/Programmierung/Backup/Discord-Bot/.env` |
| `MDDE_MD_ROOT` | `/mnt/steam-library/SteamLibrary/steamapps/workshop/content/394360/2777392649` |
| `MDDE_MOD_ROOT` | dieses Repository |
| `MDDE_HOI4_DIR` | `~/.local/share/Paradox Interactive/Hearts of Iron IV` |
| `MDDE_MODELS` | `nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free` |
| `MDDE_WORKERS` | `3` parallele Anfragen |
| `MDDE_RESERVE` | `100` Gratis-Anfragen bleiben für andere Programme übrig |

Der API-Schlüssel wird nie ausgegeben, geloggt oder ins Repository geschrieben.

## Begriffsliste

`tools/glossary.json` enthält offizielle Begriffe der deutschen HOI4-Version (abgeleitet aus
den englisch/deutschen Wertepaaren der Vanilla-Lokalisierung, z. B. *National Focus* →
*Nationaler Schwerpunkt*) und wird in den Systemprompt eingefügt.

## Tests

```bash
python3 -m pytest -q        # aus dem Repository-Wurzelordner, komplett offline
```
