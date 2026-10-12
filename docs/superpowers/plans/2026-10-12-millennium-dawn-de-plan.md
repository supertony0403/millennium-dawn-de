# Millennium Dawn DE — Implementierungsplan

Spec: `docs/superpowers/specs/2026-10-12-millennium-dawn-de-design.md`
Arbeitsweise: TDD (pytest), nur Standardbibliothek, Python ≥3.11. Tests laufen offline
(Fake-Transport statt Netz, Mini-Mod-Baum in `tests/fixtures/` bzw. `tmp_path`).
Referenz: Messskript `docs/reference/probe.py` (funktionierender Prototyp für Prompt, Token-Regex, API-Aufruf).

## Task 1 – Gerüst
- `tools/mdde/__init__.py`, `tools/mdde/__main__.py` (argparse, Unterbefehle), `pyproject.toml` (pytest-Konfig, `pythonpath = ["tools"]`).
- `descriptor.mod`: name „Millennium Dawn - Deutsche Übersetzung“, version 1.0, tags Translation/Localisation,
  `dependencies = { "Millennium Dawn: A Modern Day Mod" }`, `supported_version="1.19.*"`. Kein `path` im descriptor.
- `README.md` (Deutsch): Zweck, Umfang, Befehle, Hinweis Launcher schließen vor `install`.
- Konfiguration (`mdde/config.py`): MD_ROOT (Standard Workshop-Pfad, überschreibbar per `MDDE_MD_ROOT`),
  MOD_ROOT (Repo-Wurzel), Cache-/Log-Pfade, Modellkette `MDDE_MODELS`, `MDDE_WORKERS`, `MDDE_RESERVE`.

## Task 2 – `locfile`
- `parse(path) -> LocFile` mit Zeilenliste + Einträgen `{idx, key, ver, val, indent, trail}`; Kopfzeile `l_<lang>:` erkennen;
  BOM tolerieren; Kommentarzeilen und Leerzeilen erhalten; Wert = gierig bis zum letzten `"` der Zeile (wie NV-Pipeline).
- `render(lines) -> str` + `write(path, text)` immer mit UTF-8-BOM, `\n`-Zeilenenden.
- `escape_value(s)`: nackte `"` → `\"`, bereits escapte unverändert; echte Zeilenumbrüche verboten.
- Tests: Versionen (`key:0`, `key:`), Werte mit `\"`, Werte mit `#` innen, Trail-Kommentar, BOM rein/raus, Round-Trip identisch.

## Task 3 – `scope`
- `load_country_tags(md_root)` aus `common/country_tags/*.txt`.
- `collect(md_root, scope) -> list[Item]` mit `Item{file_rel_en, base, key, en, de_current, de_path}`.
  Regeln exakt wie Spec („offener Text“, Datei-/Schlüsselausschlüsse, Ausnahme countries/countries_cosmetic, `full`).
- Zuordnung EN↔DE über Basisnamen (`<base>_l_english.yml` ↔ `<base>_l_german.yml`), rekursiv inkl. `replace/`.
- Tests mit Mini-Baum: GER-Fokus drin, CAN-Fokus raus, `RUS_x` raus, `GER_x` drin, `USA.conservatism` raus,
  countries-Datei behält fremde Kürzel, bereits übersetzte DE-Werte raus, reine Token-Werte raus.

## Task 4 – `cache` + `validate`
- Cache: append-only JSONL, Laden in Dict `(key, sha1(en)) -> de`; Schreiben thread-sicher (Lock) und sofort flush.
  Rejects separat. Tests: Hash-Wechsel invalidiert, Duplikate → letzter gewinnt, kaputte Zeile wird übersprungen.
- Validate: Token-Regex aus probe.py; Prüfungen laut Spec; Englisch-Rest-Heuristik: nach Token-Entfernung
  ≥3 Treffer aus englischer Stoppwortliste (the, and, of, our, with, will, has, have, this, that, which, is, are, to, for)
  bei EN-Text mit ≥6 Wörtern → `"english"`. Tests je Fehlerart + gültige Fälle (Eigennamen wie „Bank of Canada“ allein lösen nicht aus).

## Task 5 – `openrouter`
- `Client(key, models, transport)`; `transport` injizierbar (Standard urllib). Request-Body wie probe.py
  (system+user, temperature 0.2, `response_format` json_object, `reasoning: {effort: low, exclude: true}`), Timeout 300 s.
- Antwort-Parsing: erstes `{…}`-Objekt aus `content` (Denkmodelle schwatzen davor).
- Modellkette + Fehlerbehandlung laut Spec; Rückgabe `{id: text}` + Statistik (Anfragen, Tokens, Fehlercodes, Modell).
- `quota()` → `GET /api/v1/key` → `free_model_daily_requests`. Schlüssel nie in Logs/Exceptions.
- Schlüssel laden: Env `OPENROUTER_API_KEY`, sonst Regex `OPENROUTER(?:_API)?_KEY` aus `MDDE_KEY_FILE`.
- Tests mit Fake-Transport: 429→Modellwechsel, alle 429→Warten (Sleep injizierbar), 404→Modell gesperrt, Schwatz-Präfix vor JSON.

## Task 6 – `translate` (Runner)
- Pakete bilden (≤9.000 Zeichen, ≤100 Texte, IDs "0","1",…), nur Items ohne Cache-Treffer.
- ThreadPool (`MDDE_WORKERS`, Standard 3); jedes gültige Ergebnis sofort in Cache.
- Budget: Quota beim Start + alle 50 Anfragen; Stopp bei `remaining <= reserve`; `--max-requests N`; `--wait-for-reset`.
- Zweite Runde für ungültige Texte in Paketen à 10; danach Rejects.
- Fortschritt zeilenweise auf stdout (flush) + `logs/translate.log`; Abschluss-JSON `logs/last_run.json`.
- Systemprompt: aus probe.py + `tools/glossary.json` (≥25 Einträge offizielle deutsche HOI4-Begriffe).
- Tests: Paketgrenzen, Cache-Treffer werden übersprungen, Budget-Stopp, zweite Runde (Fake-Client).

## Task 7 – `build` + `check`
- `build`: Ausgabe laut Spec unter `MOD_ROOT/localisation/german/…` (vorher Ordner leeren, nur generierte Dateien).
  Schreibt `build_info.json` (MD-Version aus descriptor, Zeitstempel, Zahlen).
- `check`: parst alle Ausgabedateien, BOM + Kopfzeile, Token-Gleichheit zu EN für ersetzte Werte, Vergleich MD-Version
  mit `build_info.json`; Exit-Code ≠0 bei Fehlern.
- Tests: Mini-Baum → Ausgabe enthält übersetzte Werte, MD-Übersetzungen unverändert, Zusatzschlüssel erhalten,
  `replace/`-Pfad gespiegelt, neue Datei für EN-only, fehlender Schlüssel angehängt.

## Task 8 – `install`
- Native Pfade: `~/.local/share/Paradox Interactive/Hearts of Iron IV/` (`mod/millennium_dawn_de.mod`,
  `launcher-v2.sqlite`, `dlc_load.json`). Vorlage: `../novum_vexillum_de/tools/install_into_launcher.py` (zielt dort auf Proton-Prefix – hier nativ!).
- `.mod`-Zeiger mit `path="<MOD_ROOT>"`; Mod-Zeile upserten, ins aktive Playset direkt **hinter** Millennium Dawn, aktiviert;
  `dlc_load.json` ergänzen. DB-Backup mit Zeitstempel vorher. Abbruch mit Meldung, wenn der Launcher läuft.
- Tests gegen eine Kopie der echten DB-Struktur (Schema aus der echten DB per `sqlite3 .schema` ableiten, Fixture anlegen).

## Task 9 – Abnahme (Orchestrator)
- `pytest` grün, `status` zeigt ~37.300 offene Texte (Scope germany).
- Probelauf `translate --max-requests 3`, Stichprobe prüfen, dann Volllauf mit `systemd-inhibit`.
- `build`, `check`, `install`; Spieltest durch den Nutzer; `error.log` prüfen.
