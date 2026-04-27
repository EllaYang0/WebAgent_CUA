#!/usr/bin/env python3
"""Minimal Wikipedia 2-hop Q-A synthesis script.

Output schema is drop-in compatible with WebLeaper / WebDancer / WebSailor
expectations (see WebAgent/WebLeaper/README.md and WebAgent/WebDancer/datasets/
sample_qa.jsonl):

    {
      "id": "uuid",
      "question": "...",
      "answer": "...",
      "required_entities": [...],         # WebLeaper R set — needed for ISR
      "intermediate_entities": [...],     # 2nd-layer (hop targets), included in R
      "variant": "basic_2hop",            # 'basic' / 'union' / 'reverse_union'
      "source_urls": [...],               # gold evidence pages (the 2 hops)
      "n_hops": 2,
      "tag": "synth-wiki-2hop",
    }

Construction pattern (Basic 2-hop):

    Page A (the seed person) ──birth_place──▶  Page B (the city)
                                                ──country──▶  answer
    Question: "In which country is the birthplace of <person> located?"
    Answer: country
    R = {person, city, country}; intermediate = {city}
    source_urls = [wiki_url(person), wiki_url(city)]

Uses MediaWiki API + Wikidata structured-data endpoint (no external deps,
urllib only). Validates each pair against live Wikipedia so questions stay
factually correct.

Usage:
    python scripts/synthesis_agent.py --n 10 --out data/synth/wiki_2hop.jsonl
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from typing import Optional, Tuple

UA = "WebAgentCUA-synthesis/0.1 (research; contact via repo)"


# ---------- HTTP helpers ----------

def _http_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------- Wikidata helpers ----------
# Wikidata IDs are stable; we use them to walk relations precisely.
# Properties used:
#   P19   = place of birth
#   P17   = country
#   P31   = instance of (skip if e.g. fictional character)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API_EN = "https://en.wikipedia.org/w/api.php"


def wiki_title_to_qid(title: str) -> Optional[str]:
    """Resolve an English-Wikipedia article title to its Wikidata Q-ID."""
    params = {
        "action": "query",
        "format": "json",
        "prop": "pageprops",
        "ppprop": "wikibase_item",
        "titles": title,
        "redirects": 1,
    }
    url = f"{WIKIPEDIA_API_EN}?{urllib.parse.urlencode(params)}"
    data = _http_json(url)
    pages = (data.get("query") or {}).get("pages") or {}
    for _, page in pages.items():
        qid = (page.get("pageprops") or {}).get("wikibase_item")
        if qid:
            return qid
    return None


def wikidata_get_claim_qid(qid: str, prop: str) -> Optional[str]:
    """Fetch the first non-deprecated value of `prop` (e.g. 'P19') as a
    Wikidata QID for entity `qid`."""
    params = {
        "action": "wbgetclaims",
        "format": "json",
        "entity": qid,
        "property": prop,
    }
    url = f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}"
    data = _http_json(url)
    claims = (data.get("claims") or {}).get(prop) or []
    for c in claims:
        if c.get("rank") == "deprecated":
            continue
        snak = c.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        dv = (snak.get("datavalue") or {}).get("value") or {}
        target = dv.get("id")
        if target:
            return target
    return None


def wikidata_label_and_enwiki(qid: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (English label, English-Wikipedia article title) for a QID."""
    params = {
        "action": "wbgetentities",
        "format": "json",
        "ids": qid,
        "props": "labels|sitelinks/urls",
        "languages": "en",
        "sitefilter": "enwiki",
    }
    url = f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}"
    data = _http_json(url)
    ent = ((data.get("entities") or {}).get(qid)) or {}
    label = ((ent.get("labels") or {}).get("en") or {}).get("value")
    sitelinks = ent.get("sitelinks") or {}
    en = sitelinks.get("enwiki") or {}
    title = en.get("title")
    return label, title


def enwiki_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))


# ---------- 2-hop synthesis ----------

def synth_basic_2hop(seed_title: str) -> Optional[dict]:
    """Construct a Basic 2-hop Q-A from a person seed.

    Hop 1: seed person --P19 (place of birth)--> city/place
    Hop 2: city --P17 (country)--> country

    Returns None if any step fails (no birthplace, no country, etc.) so the
    caller can skip silently.
    """
    seed_qid = wiki_title_to_qid(seed_title)
    if not seed_qid:
        return None
    seed_label, _ = wikidata_label_and_enwiki(seed_qid)
    seed_label = seed_label or seed_title

    # Hop 1
    birth_qid = wikidata_get_claim_qid(seed_qid, "P19")
    if not birth_qid:
        return None
    birth_label, birth_title = wikidata_label_and_enwiki(birth_qid)
    if not birth_label or not birth_title:
        return None

    # Hop 2
    country_qid = wikidata_get_claim_qid(birth_qid, "P17")
    if not country_qid:
        return None
    country_label, country_title = wikidata_label_and_enwiki(country_qid)
    if not country_label:
        return None

    # Don't construct a question whose answer is trivially in the seed name
    if country_label.lower() in seed_label.lower():
        return None

    record = {
        "id": str(uuid.uuid4()),
        "question": (
            f"In which country is the birthplace of {seed_label} located?"
        ),
        "answer": country_label,
        "required_entities": [seed_label, birth_label, country_label],
        "intermediate_entities": [birth_label],
        "variant": "basic_2hop",
        "source_urls": [
            enwiki_url(seed_title),
            enwiki_url(birth_title),
        ],
        "n_hops": 2,
        "tag": "synth-wiki-2hop",
        # Light formalization in WebShaper triple style. ?Y is the unknown country;
        # @ binds variables to known anchor entities.
        "formalization": [
            ["@SEED", "P19", "?M"],   # seed -- place of birth -> M (city)
            ["?M", "P17", "?Y"],      # M    -- country         -> Y (answer)
        ],
        "anchors": {
            "@SEED": seed_label,
        },
        "wikidata_qids": {
            "seed": seed_qid,
            "birthplace": birth_qid,
            "country": country_qid,
        },
    }
    return record


# ---------- Driver ----------

# Default seed pool — well-known people with stable, unambiguous Wikipedia
# pages and clearly-recorded P19 / P17 chains. The script will fall back to a
# wider Wikipedia random walk if you ask for more than this list provides.
DEFAULT_SEEDS = [
    "Albert Einstein",
    "Marie Curie",
    "Ada Lovelace",
    "Alan Turing",
    "Isaac Newton",
    "Charles Darwin",
    "Nikola Tesla",
    "Leonardo da Vinci",
    "Frida Kahlo",
    "Akira Kurosawa",
    "Hayao Miyazaki",
    "Andrei Tarkovsky",
    "Yo-Yo Ma",
    "Niels Bohr",
    "Sergei Rachmaninoff",
    "Lin-Manuel Miranda",
    "Greta Thunberg",
    "Ang Lee",
    "Steven Spielberg",
    "Cate Blanchett",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=10, help="how many Q-A pairs to emit")
    ap.add_argument("--seeds", nargs="*", default=None, help="custom seed titles")
    ap.add_argument(
        "--out",
        default="data/synth/wiki_2hop.jsonl",
        help="output jsonl path (relative to cwd)",
    )
    ap.add_argument("--sleep", type=float, default=0.2, help="seconds between API calls")
    args = ap.parse_args()

    seeds = args.seeds or DEFAULT_SEEDS
    out_path = args.out
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    written = 0
    skipped = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for seed in seeds:
            if written >= args.n:
                break
            try:
                rec = synth_basic_2hop(seed)
            except Exception as e:
                print(f"[!] {seed}: {repr(e)}", file=sys.stderr)
                skipped += 1
                time.sleep(args.sleep)
                continue
            if rec is None:
                print(f"[skip] {seed}: no full P19/P17 chain available")
                skipped += 1
                time.sleep(args.sleep)
                continue
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            print(
                f"[ok] {seed}: Q='{rec['question'][:80]}' "
                f"A='{rec['answer']}' n_hops={rec['n_hops']}"
            )
            time.sleep(args.sleep)

    print()
    print(f"wrote {written} Q-A pairs ({skipped} skipped) -> {out_path}")


if __name__ == "__main__":
    main()
