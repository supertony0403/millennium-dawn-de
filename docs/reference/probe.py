#!/usr/bin/env python3
"""THROWAWAY spike: translate one Millennium Dawn loc file EN->DE via OpenRouter free models.
Measures requests, time, token usage, validation failures. Not the real pipeline."""
import concurrent.futures as cf
import json, os, re, sys, time, urllib.request, urllib.error
from collections import Counter

MD = "/mnt/steam-library/SteamLibrary/steamapps/workshop/content/394360/2777392649/localisation"
BASE = sys.argv[1] if len(sys.argv) > 1 else "MD_focus_CAN"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out_" + BASE)
MODELS = os.environ.get("PROBE_MODELS", "nvidia/nemotron-3-super-120b-a12b:free,google/gemma-4-31b-it:free").split(",")
BATCH_CHARS = 9000
BATCH_MAX = 100
WORKERS = int(os.environ.get("PROBE_WORKERS", "3"))

LINE = re.compile(r'^\s*([\w.\-]+):\d*\s+"(.*)"\s*$')
TOKEN = re.compile(r'§!|§[A-Za-z0-9]|£[\w|]+|\$[^$\s]+\$|\[[^\]\n]+\]|\\n|@\w+')


def key():
    txt = open(os.path.expanduser("~/Documents/Programmierung/Backup/Discord-Bot/.env")).read()
    return re.search(r'OPENROUTER(?:_API)?_KEY\s*=\s*["\']?([^\s"\']+)', txt).group(1)


def load(p):
    d = {}
    with open(p, encoding="utf-8-sig", errors="replace") as fh:
        for l in fh:
            m = LINE.match(l)
            if m:
                d[m.group(1)] = m.group(2)
    return d


SYSTEM = """Du übersetzt Lokalisierungstexte der Hearts-of-Iron-IV-Mod "Millennium Dawn" (moderne Geopolitik, 2000–2050) aus dem Englischen ins Deutsche.
Regeln:
- Gib NUR ein JSON-Objekt zurück, mit exakt denselben Schlüsseln wie die Eingabe, Werte = deutsche Übersetzung.
- Platzhalter und Formatierung EXAKT unverändert übernehmen: Farbcodes §Y … §! (§ + ein Zeichen, Ende §!), Icons wie £pol_power, Variablen $NAME$, Skriptausdrücke in eckigen Klammern [ROOT.GetName], Zeilenumbrüche \\n, @TAG. Nichts davon übersetzen, verschieben ist erlaubt, löschen oder hinzufügen nicht.
- Eigennamen (Personen, Städte, Parteien, Firmen, Schiffs- und Waffennamen) im Original lassen.
- Verwende die Begriffe der offiziellen deutschen HOI4-Version: National Focus = Nationaler Fokus, Political Power = politische Macht, Civilian Factory = Zivilfabrik, Military Factory = Militärfabrik, Stability = Stabilität, War Support = Kriegsunterstützung, Decision = Entscheidung, Idea/National Spirit = Nationalgeist, Manpower = Mannstärke, Research = Forschung.
- Natürliches, flüssiges Deutsch im Stil eines Strategiespiels; Du-Ansprache vermeiden, neutral formulieren.
- Doppelte Anführungszeichen im Text als „…“ setzen."""


def call(api_key, batch, model):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "reasoning": {"effort": "low", "exclude": True},
    }
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions",
                                 data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + api_key,
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)


def parse(content):
    content = content.strip()
    m = re.search(r'\{.*\}', content, re.S)
    return json.loads(m.group(0) if m else content)


def check(src, dst):
    if not isinstance(dst, str) or not dst.strip():
        return "leer"
    if Counter(TOKEN.findall(src)) != Counter(TOKEN.findall(dst)):
        return "token"
    if "\n" in dst:
        return "zeilenumbruch"
    return None


def run_batch(api_key, batch, stats):
    for model in MODELS:
        for attempt in range(3):
            t = time.time()
            stats["requests"] += 1
            try:
                resp = call(api_key, batch, model)
            except urllib.error.HTTPError as e:
                stats[f"http_{e.code}"] += 1
                time.sleep(15 * (attempt + 1) if e.code == 429 else 5)
                if e.code == 404:
                    break
                continue
            except Exception as e:
                stats["exc"] += 1
                time.sleep(5)
                continue
            stats["sec"] += time.time() - t
            u = resp.get("usage") or {}
            stats["tok_in"] += u.get("prompt_tokens", 0)
            stats["tok_out"] += u.get("completion_tokens", 0)
            try:
                out = parse(resp["choices"][0]["message"]["content"])
            except Exception:
                stats["json_fail"] += 1
                continue
            res = {}
            for k, v in batch.items():
                err = check(v, out.get(k))
                if err:
                    stats["bad_" + err] += 1
                else:
                    res[k] = out[k]
            stats["model_" + model] += 1
            return res
    return {}


def main():
    en = load(f"{MD}/english/{BASE}_l_english.yml")
    de = load(f"{MD}/german/{BASE}_l_german.yml")
    todo = {k: v for k, v in en.items()
            if (k not in de or de[k] == v) and re.search(r'[A-Za-z]{3}', TOKEN.sub('', v))}
    ids = {str(i): k for i, k in enumerate(todo)}
    batches, cur, size = [], {}, 0
    for i, k in ids.items():
        v = todo[k]
        if cur and (size + len(v) > BATCH_CHARS or len(cur) >= BATCH_MAX):
            batches.append(cur); cur, size = {}, 0
        cur[i] = v; size += len(v)
    if cur:
        batches.append(cur)
    print(f"{BASE}: {len(todo)} Texte, {sum(map(len, todo.values()))} Zeichen, {len(batches)} Pakete", flush=True)
    api_key = key()
    stats = Counter()
    done = {}
    t0 = time.time()
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for res in ex.map(lambda b: run_batch(api_key, b, stats), batches):
            done.update(res)
            print(f"  {len(done)}/{len(todo)} ok, {stats['requests']} Anfragen, {time.time()-t0:.0f}s", flush=True)
    wall = time.time() - t0
    result = {ids[i]: {"en": todo[ids[i]], "de": v} for i, v in done.items()}
    json.dump(result, open(OUT + ".json", "w"), ensure_ascii=False, indent=1)
    s = dict(stats); s["wall_sec"] = round(wall); s["ok"] = len(done); s["todo"] = len(todo)
    s["batches"] = len(batches)
    json.dump(s, open(OUT + "_stats.json", "w"), indent=1)
    print(json.dumps(s, indent=1))


if __name__ == "__main__":
    main()
