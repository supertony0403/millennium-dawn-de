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
| `germany` (Standard) | alles außer fremden Fokusbäumen (`MD_focus_<TAG>`), Namenslisten und Schlüsseln mit fremdem Länderkürzel (`RUS_…`, `USA.…`) – Ausnahme Ländernamen-Dateien (`countries`, `MD_countries_cosmetic`, `replaced_from_vanilla_countries_cosmetic`) | 39.622 (32.840 verschiedene Texte, ≈ 443 Anfragen) |
| `full` | alles außer Namenslisten (`victory_points`, `state_names`, `MD_terrain_cities`, `strategic_region_names`, `supply_area_names`) | 175.546 (≈ 2.082 Anfragen) |

Gleiche englische Texte unter verschiedenen Schlüsseln werden nur einmal angefragt.

„Offen“ ist ein englischer Text, der ohne Formatierungs-Tokens noch mindestens drei
aufeinanderfolgende Buchstaben enthält und dessen deutscher Wert bei Millennium Dawn fehlt,
(bis auf Groß-/Kleinschreibung und Leerraum) identisch mit dem englischen ist oder eine
veraltete englische Kopie ist (≥ 3 englische und ≤ 1 deutsche Stoppwörter, oder > 60 % der
kleingeschriebenen Wörter stammen aus dem englischen Text und kein deutsches Stoppwort).
Gegen die echten Daten geprüft: 207 solcher Kopien, 0 Fehlalarme unter 16.928 eindeutig
deutschen MD-Werten.

## Aufruf

Alle Befehle laufen aus dem Ordner `tools/` (nur Python-Standardbibliothek, Python ≥ 3.11):

```bash
cd tools
python3 -m mdde status --scope germany   # offene Texte, Cache-Stand, Tageskontingent
python3 -m mdde status --scope full --no-quota
python3 -m mdde translate                # übersetzt offene Texte (Scope germany)
python3 -m mdde translate --max-requests 3          # Probelauf
python3 -m mdde translate --wait-for-reset          # bei leerem Kontingent bis 00:00 UTC warten
python3 -m mdde translate --retry-unchanged         # unveränderte Antworten erneut anfragen
python3 -m mdde translate --retry-unchanged=translatable  # … nur die, die noch übersetzbar wirken
python3 -m mdde revalidate               # Cache mit dem aktuellen Validator prüfen (Probelauf)
python3 -m mdde revalidate --apply       # … und bereinigen (vorher Backup)
python3 -m mdde revalidate --repair-rejects          # doppelt escapte \\n in rejects.jsonl reparieren (Probelauf)
python3 -m mdde revalidate --repair-rejects --apply  # … und gültige in den Cache übernehmen (vorher Backup)
python3 -m mdde build                    # erzeugt localisation/german/** + build_info.json
python3 -m mdde check                    # prüft die Ausgabe (Exit 0 ok, 1 Fehler, 2 veraltet)
python3 -m mdde install --yes            # trägt den Submod im Launcher ein
```

Lange Übersetzungsläufe vor dem Standby schützen:
`systemd-inhibit --what=sleep:idle python3 -m mdde translate --wait-for-reset`

### Ablauf

1. `status` – zeigt, wie viel zu tun ist (und wie viele Anfragen das ungefähr kostet).
2. `translate` – jedes gültige Ergebnis landet sofort in `cache/translations.jsonl`;
   Antworten, die dem Englischen gleichen, landen in `cache/unchanged.jsonl` und werden
   nur mit `--retry-unchanged` (alle) bzw. `--retry-unchanged=translatable` erneut
   angefragt. Wirkt ein solcher Text noch übersetzbar (kleingeschriebene oder englische
   Funktionswörter wie „of“, „the“, „Toggle“, „Cost“, Formatierung plus Wörter), fragt
   Runde zwei ihn zuerst beim nächsten Modell der Kette erneut an; erst wenn er auch dort
   unverändert bleibt, kommt er nach `unchanged.jsonl`. Eigennamen („ExxonMobil“,
   „Maghreb“, „Opposition“) gehen direkt dorthin. Ungültige Texte bekommen eine zweite Runde
   in kleinen Paketen, danach landen sie in `cache/rejects.jsonl` (im Spiel bleibt dann der
   englische Text stehen). Ein Paket, dessen Antworten verschoben wirken, wird ganz
   verworfen und kleiner wiederholt; sperrt die Inhaltsmoderation des Anbieters ein Paket
   (HTTP 403 mit Moderationsdaten), wird es geteilt, bis der eine Text isoliert ist – der
   kommt mit Grund `moderation` in die Ablehnungen, das Modell bleibt in der Kette.
   Das Tageskontingent wird alle 10 Anfragen abgefragt und dazwischen lokal
   heruntergezählt, nahe der Reserve vor jeder Anfrage; der Lauf stoppt, bevor die Reserve
   angetastet wird – nach bestem Bemühen: Was andere Programme mit demselben Schlüssel
   (z. B. der Discord-Bot) verbrauchen, wird gemessen und vorausgeschätzt, ein verzögert
   zu hoher Serverwert hebt die Schätzung nie an; eine Garantie ist das nicht. Ist das Kontingent nicht abfragbar, startet der Lauf
   gar nicht erst (außer mit `--allow-unknown-quota`). Strg+C oder SIGTERM: keine neuen
   Anfragen mehr, laufende Antworten werden noch gesichert. Es läuft immer nur ein
   `translate` gleichzeitig (Sperre `cache/translate.lock`). Die Fortschrittszeile zeigt
   Uhrzeit und geschätzte Restdauer der Runde.
3. `revalidate` (optional) – prüft alle Cache-Einträge mit dem aktuellen Validator gegen
   den aktuellen englischen Text. Standard ist ein Probelauf mit Zahlen und Beispielen je
   Grund; `--apply` legt `translations.jsonl.bak-<UTC-Zeit>` an, schreibt den bereinigten
   Cache und verschiebt ungültige Einträge nach `rejects.jsonl`, unveränderte nach
   `unchanged.jsonl`. Nur ohne laufenden `translate` ausführen.
   Antworten mit doppelt escapten Backslashes (`\\n` statt `\n`, `\\"` statt `\"`) repariert
   `translate` vor der Prüfung selbst; ältere solche Ablehnungen holt
   `revalidate --repair-rejects` offline aus `rejects.jsonl` (Standard Probelauf, `--apply`
   hängt die gültigen nach einem Backup an den Cache an; der Cache hat Vorrang vor
   `rejects.jsonl`).
4. `build` – spiegelt **jede** deutsche Datei von Millennium Dawn unter exakt demselben
   relativen Pfad (inkl. `replace/`) und ersetzt nur offene Werte durch die Übersetzung.
   Englische Dateien ohne deutsches Gegenstück werden als neue `_l_german.yml` angelegt,
   fehlende Schlüssel ans Dateiende gehängt – jeder Schlüssel in genau einer Datei. Ein noch
   unübersetzter Schlüssel, für den HOI4 selbst einen deutschen Text hat
   (`MDDE_VANILLA_DE`), wird nicht englisch angehängt.
5. `check` – parst alle Ausgabedateien (BOM, Kopfzeile `l_german:`, Token-Gleichheit,
   Schlüssel in mehreren Ausgabedateien) und warnt, wenn Millennium Dawn seit dem letzten
   Build aktualisiert wurde. Doppel-Schlüssel, die schon Millennium Dawn selbst so hat
   (z. B. `MD_subideology_parties` / `MD_politics_view_parties`), erscheinen als Warnung,
   ändern den Exit-Code aber nicht.
6. `install` – **Paradox-Launcher vorher schließen!** Schreibt den Zeiger
   `mod/millennium_dawn_de.mod`, legt ein Backup von `launcher-v2.sqlite` an, trägt den
   Submod im aktiven Playset direkt **hinter** Millennium Dawn ein und ergänzt
   `dlc_load.json`. Mehrfach ausführbar.

Nach einem Update von Millennium Dawn: `status` → `translate` → `build` → `check`.

Exit-Codes: `check` 0 = in Ordnung, 1 = Fehler in der Ausgabe, 2 = Build veraltet
(Millennium Dawn hat sich seit dem letzten `build` geändert). `translate` endet mit 1 bei
Schlüssel-/Modellfehlern (auch: nur noch dauerhaft rate-limitierte Modelle), unbekanntem
Kontingent oder wenn schon ein Lauf aktiv ist, mit 130 nach Strg+C/SIGTERM; ein Stopp wegen
der Reserve oder `--max-requests` ist kein Fehler.

## Dateien

| Pfad | Inhalt |
|---|---|
| `tools/mdde/` | Python-Paket (`config`, `locfile`, `scope`, `cache`, `validate`, `openrouter`, `translate`, `revalidate`, `build`, `install`, `cli`) |
| `cache/translations.jsonl` | Übersetzungs-Cache (append-only, Treffer nur bei gleichem SHA-1 des englischen Textes) |
| `cache/rejects.jsonl` | Texte, die auch nach der zweiten Runde ungültig waren (mit Grund) |
| `cache/unchanged.jsonl` | Texte, die das Modell unverändert zurückgab (Schlüssel + SHA-1) |
| `cache/translate.lock` | Sperre gegen parallele Läufe |
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
| `MDDE_WORKERS` | `6` parallele Anfragen |
| `MDDE_RESERVE` | `100` Gratis-Anfragen bleiben für andere Programme übrig (nach bestem Bemühen) |
| `MDDE_CACHE_DIR` | `cache/` dieses Repositorys (z. B. auf eine Kopie zeigen lassen) |
| `MDDE_VANILLA_DE` | `/mnt/steam-library/SteamLibrary/steamapps/common/Hearts of Iron IV/localisation/german` |

Der API-Schlüssel wird nie ausgegeben, geloggt oder ins Repository geschrieben; Schlüssel
mit anderen Zeichen als Buchstaben, Ziffern, `_` und `-` (z. B. ein Zeilenumbruch) werden
beim Laden abgelehnt.

## Begriffsliste

`tools/glossary.json` enthält offizielle Begriffe der deutschen HOI4-Version (abgeleitet aus
den englisch/deutschen Wertepaaren der Vanilla-Lokalisierung, z. B. *National Focus* →
*Nationaler Schwerpunkt*) und wird in den Systemprompt eingefügt.

## Tests

```bash
python3 -m pytest -q        # aus dem Repository-Wurzelordner, komplett offline
```

`tests/test_real_md.py` prüft zusätzlich gegen die installierte Millennium-Dawn-Kopie
(veraltete englische Kopien, Fehlalarme, byte-identischer Rundlauf aller Dateien) und wird
übersprungen, wenn sie fehlt.
