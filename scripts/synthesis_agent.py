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
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Optional, Tuple

UA = "WebAgentCUA-synthesis/0.1 (research; contact via repo)"

WIKIDATA_SPARQL = "https://query.wikidata.org/sparql"


# ============================================================
#  LLM 预筛 — 用 Gemini 无工具回答,答对就丢掉这条 Q-A
# ============================================================
#
# 背景:之前 wiki_2hop_obscure_rev 跑完后 31% 的"成功" trajectory 是 0 工具直接
# 答(LLM 凭训练数据背出来)。这些样本作为训练数据没价值——模型学不到 web
# 检索行为。本筛器在合成阶段就把它们拦下来。
#
# 实现:复用 agent 主 stack 的同一个 Vertex AI / Gemini endpoint(同一个模型),
# 用极简 prompt 让模型"凭记忆答一个短语,不会就说 unknown"。如果答案与 ground
# truth 双向 substring 命中,即视为 LLM 能猜中,丢弃。

# 缓存创建的 creds 避免每次都刷 token
_screen_creds = None


def _get_vertex_token():
    global _screen_creds
    import google.auth
    import google.auth.transport.requests as gar
    if _screen_creds is None:
        _screen_creds, _ = google.auth.default(
            scopes=['https://www.googleapis.com/auth/cloud-platform']
        )
    req = gar.Request()
    _screen_creds.refresh(req)
    return _screen_creds.token


# Dual-prompt screener (v3): the original single-prompt screener with
# "answer 'unknown' if you don't know" was too conservative — it let through
# questions Gemini knew but chose to hedge on. v2 run revealed one such miss
# (Anton Alekseev → Ludvig Faddeev: screener said "unknown" but solver run
# answered correctly with 0 tools). v3 runs TWO prompts:
#   A. conservative — keep the original behavior to catch confident knowledge
#   B. aggressive — force Gemini to commit to a best guess
# If EITHER catches it, mark guessable. This doubles API cost but plugs the
# leak observed in v2 (Anton Alekseev case).

_SCREENER_PROMPT_CONSERVATIVE = (
    "Answer the following question in ONE short phrase using ONLY your prior knowledge. "
    "Do NOT search the web. Do NOT explain. If you do not know, answer exactly 'unknown'.\n\n"
    "Question: {q}\n"
    "Answer:"
)

_SCREENER_PROMPT_AGGRESSIVE = (
    "You have broad knowledge of historical figures, scientists, and academic genealogies. "
    "For the question below, commit to a specific answer based on your training data. "
    "Do NOT say 'unknown' or 'I'm not sure' — pick the single most likely answer. "
    "Output ONLY the answer, no preamble.\n\n"
    "Question: {q}"
)


def _normalize_for_match(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _is_substring_match(expected: str, answer_text: str) -> bool:
    """Bidirectional substring match ignoring punctuation + case."""
    e = _normalize_for_match(expected)
    a = _normalize_for_match(answer_text)
    if not e:
        return False
    if e in a:
        return True
    # Also accept the reverse direction, but only if answer is non-trivial
    if a and len(a) >= 3 and a in e:
        return True
    return False


def _call_gemini_screener(prompt_template: str, question: str, expected_answer: str,
                           model: str, project: str, timeout: float = 60.0,
                           temperature: float = 0.0) -> Tuple[bool, str]:
    """Single-prompt call. Returns (guessable, raw_response)."""
    url = (
        f"https://aiplatform.googleapis.com/v1beta1/projects/{project}"
        "/locations/global/endpoints/openapi/chat/completions"
    )
    payload = {
        "model": model,
        "max_tokens": 80,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt_template.format(q=question)}],
    }
    try:
        token = _get_vertex_token()
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
    except Exception as e:
        # Network / 429 / auth issue — conservatively say "can't guess" so we
        # keep the candidate. Better to over-include than over-exclude on errors.
        return False, f"<screen_error:{type(e).__name__}>"

    if not text:
        return False, text
    # The conservative prompt may legitimately produce "unknown"; treat as no-guess
    if text.lower().strip().strip(".") in ("unknown", "i don't know", "i do not know",
                                            "i'm not sure", "i am not sure"):
        return False, text
    return _is_substring_match(expected_answer, text), text


def llm_can_answer_without_tools(question: str, expected_answer: str,
                                  model_name: Optional[str] = None,
                                  project_id: Optional[str] = None,
                                  timeout: float = 60.0,
                                  aggressive_samples: int = 3) -> Tuple[bool, str]:
    """v3 multi-sample screener.

    Process:
      1. Conservative prompt @ temp=0.0 (1 call). If it commits to the answer, done.
      2. Otherwise, aggressive prompt @ temp=0.8, sampled N times. If ANY sample
         matches the expected answer, mark guessable.

    Rationale: in the v2 leak case, Gemini knew Anton Alekseev's advisor was
    Ludvig Faddeev but only got it ~1-in-3 samples; single-shot screening
    missed it. N-sample voting captures these unstable cases.

    Cost: 1 + N API calls per question (default 4). Heavier than v0 but
    accurate enough to plug the observed leak.

    Returns (guessable, "A=<conservative_result> | B[1..N]=<aggressive_samples>").
    """
    model = model_name or os.environ.get("MODEL_NAME", "google/gemini-3.1-pro-preview")
    project = project_id or os.environ.get("GCP_PROJECT_ID", "msagentrt")

    # 1. Conservative pass
    g_a, r_a = _call_gemini_screener(_SCREENER_PROMPT_CONSERVATIVE,
                                      question, expected_answer, model, project,
                                      timeout=timeout, temperature=0.0)
    if g_a:
        return True, f"A=GUESSED({r_a[:60]!r}) | B=skipped"

    # 2. Aggressive multi-sample
    b_samples = []
    any_match = False
    for i in range(aggressive_samples):
        g_b, r_b = _call_gemini_screener(_SCREENER_PROMPT_AGGRESSIVE,
                                          question, expected_answer, model, project,
                                          timeout=timeout, temperature=0.8)
        b_samples.append((g_b, r_b))
        if g_b:
            any_match = True
            break  # one hit is enough

    b_summary = " | ".join(
        f"B{idx+1}={'HIT' if hit else 'no'}({resp[:40]!r})"
        for idx, (hit, resp) in enumerate(b_samples)
    )
    return any_match, f"A=NO({r_a[:40]!r}) | {b_summary}"


def fetch_sparql_seeds(
    occupation_qids=None,
    sitelink_min=5,
    sitelink_max=25,
    born_after_year=1850,
    born_before_year=2000,
    limit=100,
) -> list:
    """Pull a list of "less-famous-but-real" Wikipedia article titles via SPARQL.

    Why filter on sitelinks: it is the simplest fame proxy on Wikidata. Einstein
    has 200+ language editions; a competent-but-obscure mid-century mathematician
    has 5-25. Forcing sitelinks into [sitelink_min, sitelink_max] gives us seeds
    Gemini probably cannot recognise from a few clues, so the agent has to
    really search.

    occupation_qids: list of Wikidata QIDs to constrain occupation. Helpful values:
        Q170790 = mathematician
        Q169470 = physicist
        Q593644 = chemist
        Q901    = scientist (parent class — broad)
        Q36180  = writer
        Q1028181 = painter
        Q49757  = poet
        Q482980 = author
    """
    occ_clause = ""
    if occupation_qids:
        values = " ".join(f"wd:{q}" for q in occupation_qids)
        occ_clause = (
            f"VALUES ?occ {{ {values} }}\n"
            "  ?person wdt:P106 ?occ ."
        )
    query = f"""
SELECT DISTINCT ?person ?personLabel ?article WHERE {{
  ?person wdt:P31 wd:Q5 ;
          wdt:P19 ?pob ;
          wdt:P569 ?dob ;
          wikibase:sitelinks ?sl .
  {occ_clause}
  FILTER(YEAR(?dob) >= {born_after_year} && YEAR(?dob) <= {born_before_year})
  FILTER(?sl >= {sitelink_min} && ?sl <= {sitelink_max})
  ?pob wdt:P17 ?country .
  ?article schema:about ?person ;
           schema:isPartOf <https://en.wikipedia.org/> .
  SERVICE wikibase:label {{ bd:serviceParam wikibase:language "en". }}
}}
LIMIT {limit}
""".strip()
    url = f"{WIKIDATA_SPARQL}?{urllib.parse.urlencode({'query': query, 'format': 'json'})}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/sparql-results+json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read().decode("utf-8"))
    titles = []
    seen = set()
    for b in (data.get("results") or {}).get("bindings") or []:
        article = (b.get("article") or {}).get("value", "")
        if not article or "/wiki/" not in article:
            continue
        title = urllib.parse.unquote(article.split("/wiki/", 1)[1]).replace("_", " ")
        if title not in seen:
            seen.add(title)
            titles.append(title)
    return titles


# ---------- HTTP helpers ----------

def _http_json(url: str, timeout: float = 20.0, max_retries: int = 6):
    """GET + parse JSON. Retries on 429 / 5xx with exponential backoff;
    Wikidata returns 429 aggressively when sustaining >~1 req/s.
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    delay = 1.0
    last_err = None
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (429, 500, 502, 503, 504):
                # Honour Retry-After header if present.
                ra = e.headers.get("Retry-After") if e.headers else None
                wait = float(ra) if (ra and ra.replace(".", "", 1).isdigit()) else delay
                wait = min(wait, 30.0)
                print(f"  [http] {e.code} on attempt {attempt+1}; sleeping {wait:.1f}s")
                time.sleep(wait)
                delay = min(delay * 2, 30.0)
                continue
            raise
        except urllib.error.URLError as e:
            last_err = e
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise last_err if last_err else RuntimeError(f"_http_json: {url} failed after {max_retries} retries")


# ---------- Wikidata helpers ----------
# Wikidata IDs are stable; we use them to walk relations precisely.
# Properties used:
#   P19   = place of birth
#   P17   = country
#   P31   = instance of (skip if e.g. fictional character)

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
WIKIPEDIA_API_EN = "https://en.wikipedia.org/w/api.php"


# ============================================================
#  Category-based seed fetch — SPARQL-free path
# ============================================================
#
# Why this exists: Wikidata SPARQL (WDQS) goes down for hours at a time and
# rate-limits aggressively. Category API + wbgetentities run on different
# infrastructure and stay up. This gives us a reliable backup that's also more
# topically precise — you can target "Soviet mathematicians born 1900-1950"
# directly via Category:Soviet_mathematicians instead of guessing occupation
# QIDs.


def _wiki_category_members(category: str, max_pages: int = 500,
                            recurse_depth: int = 0, ns_filter: int = 0) -> list:
    """List article titles under a Wikipedia category. ns_filter=0 → article
    space only (no User: / Talk: noise). Optional recursion into sub-categories.
    Returns deduplicated titles."""
    seen = set()
    out = []

    def _fetch(cat: str, depth: int):
        if len(out) >= max_pages:
            return
        cont = None
        while len(out) < max_pages:
            params = {
                "action": "query",
                "format": "json",
                "list": "categorymembers",
                "cmtitle": cat,
                "cmlimit": "500",
                "cmtype": "page|subcat" if depth > 0 else "page",
            }
            if cont:
                params["cmcontinue"] = cont
            url = f"{WIKIPEDIA_API_EN}?{urllib.parse.urlencode(params)}"
            data = _http_json(url)
            members = (data.get("query") or {}).get("categorymembers") or []
            for m in members:
                if len(out) >= max_pages:
                    break
                ns = m.get("ns")
                title = m.get("title")
                if ns == ns_filter and title not in seen:
                    seen.add(title)
                    out.append(title)
                elif ns == 14 and depth > 0:  # subcategory
                    _fetch(title, depth - 1)
            cont = (data.get("continue") or {}).get("cmcontinue")
            if not cont:
                break

    _fetch(category, recurse_depth)
    return out


def _wikidata_sitelink_count(qid: str) -> Optional[int]:
    """Return how many language Wikipedia editions the entity appears in.
    Used as the same fame proxy as SPARQL's `wikibase:sitelinks`."""
    params = {
        "action": "wbgetentities",
        "format": "json",
        "ids": qid,
        "props": "sitelinks",
    }
    url = f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}"
    data = _http_json(url)
    ent = ((data.get("entities") or {}).get(qid)) or {}
    sitelinks = ent.get("sitelinks") or {}
    # Count only language wikis (skip wikiquote, wikinews, etc.)
    return sum(1 for k in sitelinks if k.endswith("wiki") and not k.endswith(("quotewiki", "newswiki", "sourcewiki", "booksowiki", "voywiki", "versitywiki")))


def fetch_category_seeds(
    categories: list,
    sitelink_min: int = 3,
    sitelink_max: int = 10,
    max_per_category: int = 200,
    recurse_depth: int = 0,
    limit: int = 100,
    sleep: float = 0.1,
) -> list:
    """SPARQL-free seed fetch: walks one or more Wikipedia categories, then
    filters each candidate by Wikidata sitelink count to keep it within the
    obscurity band.

    Returns up to `limit` deduplicated titles. Categories must be passed with
    the 'Category:' prefix (e.g. 'Category:20th-century_Russian_mathematicians').
    """
    candidates = []
    seen = set()
    for cat in categories:
        if not cat.startswith("Category:"):
            cat = "Category:" + cat.replace(" ", "_")
        members = _wiki_category_members(cat, max_pages=max_per_category, recurse_depth=recurse_depth)
        print(f"  [cat] {cat}: {len(members)} raw members")
        for t in members:
            if t not in seen:
                seen.add(t)
                candidates.append(t)

    print(f"  [cat] total unique candidates before sitelink filter: {len(candidates)}")
    kept = []
    for t in candidates:
        if len(kept) >= limit:
            break
        try:
            qid = wiki_title_to_qid(t)
            if not qid:
                continue
            sl = _wikidata_sitelink_count(qid)
            if sl is None:
                continue
            if sitelink_min <= sl <= sitelink_max:
                kept.append(t)
        except Exception as e:
            # Network blip; skip this candidate
            print(f"  [cat] err on {t}: {repr(e)[:80]}")
        time.sleep(sleep)
    print(f"  [cat] kept {len(kept)} after sitelink filter [{sitelink_min},{sitelink_max}]")
    return kept


# A small curated list of "obscure-ish people" categories useful as defaults.
# Picked so that the entities inside have specific occupational + national
# context but aren't household names.
DEFAULT_CATEGORIES = [
    "Category:20th-century Russian mathematicians",
    "Category:20th-century German physicists",
    "Category:Soviet mathematicians",
    "Category:Czech mathematicians",
    "Category:Hungarian physicists",
    "Category:Italian chemists",
    "Category:20th-century Polish mathematicians",
    "Category:20th-century Swiss physicists",
]


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


def wikidata_get_claim_qid(qid: str, prop: str, current_only: bool = False) -> Optional[str]:
    """Fetch the first non-deprecated value of `prop` (e.g. 'P19') as a
    Wikidata QID for entity `qid`. Pass current_only=True for P17 country
    so historical successors (e.g. Roman Empire for London) are skipped."""
    qids = wikidata_get_claim_qids(qid, prop, max_count=1, current_only=current_only)
    return qids[0] if qids else None


def wikidata_get_claim_qids(qid: str, prop: str, max_count: int = 5,
                            current_only: bool = False) -> list:
    """Fetch up to `max_count` non-deprecated QID values of `prop` for entity `qid`.

    If `current_only` is True, skip claims that carry a P582 (end time)
    qualifier — i.e. discard relationships that have ended. Critical for P17
    (country) on cities: London's P17 lists historical countries (Roman
    Empire, etc.) BEFORE the modern UK if you don't filter.
    """
    params = {
        "action": "wbgetclaims",
        "format": "json",
        "entity": qid,
        "property": prop,
    }
    url = f"{WIKIDATA_API}?{urllib.parse.urlencode(params)}"
    data = _http_json(url)
    claims = (data.get("claims") or {}).get(prop) or []
    # Sort: prefer rank=preferred, then normal, then anything else; within a
    # rank tier preserve original order. This puts the "best" current value
    # first when current_only filter is on.
    rank_order = {"preferred": 0, "normal": 1, "deprecated": 2}
    claims = sorted(claims, key=lambda c: rank_order.get(c.get("rank"), 3))
    out = []
    for c in claims:
        if c.get("rank") == "deprecated":
            continue
        if current_only:
            quals = c.get("qualifiers") or {}
            if quals.get("P582"):
                # has an end-time qualifier → relationship is historical, skip
                continue
        snak = c.get("mainsnak") or {}
        if snak.get("snaktype") != "value":
            continue
        dv = (snak.get("datavalue") or {}).get("value") or {}
        target = dv.get("id")
        if target:
            out.append(target)
            if len(out) >= max_count:
                break
    return out


def wikidata_get_claim_year(qid: str, prop: str) -> Optional[int]:
    """For time-typed properties (e.g. P569 birth date), return year as int."""
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
        t = dv.get("time")  # e.g. "+1879-03-14T00:00:00Z"
        if t:
            try:
                yr = int(t.lstrip("+").split("-")[0])
                return yr
            except Exception:
                pass
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

# Properties used to build "describe-without-naming" clues for the Reverse-Union
# variant. None of these reveal the entity name itself.
CLUE_PROPS = {
    "P106": "occupation",          # e.g. "physicist"
    "P166": "award received",      # e.g. "Nobel Prize in Physics"
    "P800": "notable work",        # e.g. "theory of relativity"
    "P135": "movement",            # e.g. "Cubism"
    "P136": "genre",               # e.g. "science fiction"
    "P39":  "position held",       # e.g. "President of the United States"
    "P184": "doctoral advisor",
    "P802": "student",
    "P101": "field of work",       # e.g. "physics"
}


# v3: clue-strength tiers. v2 run showed that ~60% of failed tasks came from
# "weak-only" clue sets like "mathematician + 1910s" matching thousands of
# entities. v3 categorizes each Wikidata property by how uniquely it identifies
# a person, and requires the final clue set to contain enough strong signal.

CLUE_STRENGTH = {
    # STRONG — typically narrow the candidate set to <50 entities
    "P800":         "STRONG",   # notable work title (e.g. "On the Origin of Species")
    "P166":         "STRONG",   # award received (e.g. "Salem Prize")
    "P184":         "STRONG",   # doctoral advisor (specific person)
    "P39":          "STRONG",   # position held (e.g. "Director of MIT Media Lab")

    # MEDIUM — narrows to hundreds
    "P108":         "MEDIUM",   # employer / institution
    "P69":          "MEDIUM",   # educated at
    "P802":         "MEDIUM",   # students of (reverse advisor)

    # WEAK — narrows to thousands at best
    "P106":         "WEAK",     # occupation (mathematician, physicist)
    "P101":         "WEAK",     # field of work (algebra, optics)
    "P135":         "WEAK",     # movement
    "P136":         "WEAK",     # genre

    # VERY WEAK — only useful in combination
    "P569_decade":  "VERY_WEAK",  # birth decade
}

# Default uniqueness requirement: a candidate must have at least 1 STRONG
# clue OR at least 2 MEDIUM clues. Otherwise the clue set is too ambiguous
# for the agent to uniquely identify the seed person.
def clue_set_strength_ok(clue_records: list) -> Tuple[bool, str]:
    """clue_records is a list of (text, prop_id) tuples. Returns (ok, reason).
    Rule: need >=1 STRONG, OR >=2 MEDIUM, OR (>=1 MEDIUM + >=2 WEAK)."""
    strong = sum(1 for _, p in clue_records if CLUE_STRENGTH.get(p) == "STRONG")
    medium = sum(1 for _, p in clue_records if CLUE_STRENGTH.get(p) == "MEDIUM")
    weak   = sum(1 for _, p in clue_records if CLUE_STRENGTH.get(p) == "WEAK")
    if strong >= 1:
        return True, f"strong={strong}"
    if medium >= 2:
        return True, f"medium={medium}"
    if medium >= 1 and weak >= 2:
        return True, f"medium={medium}+weak={weak}"
    return False, f"too_weak(strong={strong} medium={medium} weak={weak})"


def collect_clues_for(seed_qid: str, banned_labels: set, max_clues: int = 5) -> list:
    """v3: returns a list of (clue_text, prop_id) tuples (was: list of strings).

    The prop_id lets the caller check clue-set strength via
    `clue_set_strength_ok()`. Callers that only need clue text can do
    `[c[0] for c in collect_clues_for(...)]` — but the v2-specific synth path
    needs the prop_ids to enforce the strength requirement.
    """
    clues = []  # list of (text, prop_id)

    def add_set_clue(prop: str, template: str, max_vals: int = 2):
        """Resolve up to max_vals QIDs of prop, format with template."""
        if len(clues) >= max_clues:
            return
        qids = wikidata_get_claim_qids(seed_qid, prop, max_count=max_vals + 2)
        labels = []
        for q in qids:
            if len(labels) >= max_vals:
                break
            lbl, _ = wikidata_label_and_enwiki(q)
            if lbl and not _label_banned(lbl, banned_labels):
                labels.append(lbl)
        if not labels:
            return
        joined = " and ".join(labels) if len(labels) > 1 else labels[0]
        clues.append((template.format(joined), prop))

    # Occupation (P106) — WEAK lead but commonly available
    add_set_clue("P106", "works as a {}", max_vals=2)

    # Field of work (P101) — WEAK but adds specificity
    add_set_clue("P101", "works in {}", max_vals=2)

    # Notable work (P800) — STRONG
    works = wikidata_get_claim_qids(seed_qid, "P800", max_count=3)
    for q in works:
        if len(clues) >= max_clues:
            break
        lbl, _ = wikidata_label_and_enwiki(q)
        if lbl and not _label_banned(lbl, banned_labels):
            clues.append((f'is best known for the work "{lbl}"', "P800"))

    # Awards received (P166) — STRONG
    add_set_clue("P166", "received the {}", max_vals=2)

    # Position held (P39) — STRONG when specific
    add_set_clue("P39", "held the position of {}", max_vals=1)

    # Doctoral advisor (P184) — STRONG (academic lineage)
    add_set_clue("P184", "studied under {}", max_vals=1)

    # Employer (P108) — MEDIUM
    add_set_clue("P108", "worked at {}", max_vals=1)

    # Educated at (P69) — MEDIUM
    add_set_clue("P69", "was educated at {}", max_vals=1)

    # Birth decade (P569) — VERY WEAK
    if len(clues) < max_clues:
        yr = wikidata_get_claim_year(seed_qid, "P569")
        if yr:
            decade = (yr // 10) * 10
            clues.append((f"was born in the {decade}s", "P569_decade"))

    return clues[:max_clues]


def collect_clue_texts_for(seed_qid: str, banned_labels: set, max_clues: int = 5) -> list:
    """Backwards-compatible wrapper that returns just the text strings.
    Use this for old reverse_union path that doesn't care about prop_id."""
    return [c[0] for c in collect_clues_for(seed_qid, banned_labels, max_clues)]


def _label_banned(label: str, banned: set) -> bool:
    if not label:
        return True
    lo = label.lower()
    for b in banned:
        if not b:
            continue
        b_lo = b.lower()
        if lo == b_lo or lo in b_lo or b_lo in lo:
            return True
    return False


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
    country_qid = wikidata_get_claim_qid(birth_qid, "P17", current_only=True)
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


# ============================================================
#  v2: specific-entity answers (no country) + BAN + freq cap
# ============================================================
#
# Why: v0 hit 100% country-answer leakage rate (Russian mathematicians →
# Russia). Gemini guesses the country from a seed's name pattern regardless of
# obscurity. v2 switches the answer to a specific entity (city / advisor /
# institution / educated_at / notable_work) so the answer space is huge and
# specific-entity knowledge is much sparser in LLM memory.
#
# See /scr/rucnyz/.claude/projects/-scr-rucnyz-projects/memory/synthesis_v2_design.md
# for the decision rationale.

# Answer priority chain — try each in order; pick first non-banned hit.
V2_ANSWER_PROPS = [
    {
        "prop": "P19",
        "name": "birthplace_city",
        "template_reverse": "In which city was this person born?",
    },
    {
        "prop": "P184",
        "name": "doctoral_advisor",
        "template_reverse": "Who was the doctoral advisor of this person?",
    },
    {
        "prop": "P108",
        "name": "employer",
        "template_reverse": "At which institution did this person work?",
    },
    {
        "prop": "P69",
        "name": "educated_at",
        "template_reverse": "At which institution did this person study?",
    },
    {
        "prop": "P800",
        "name": "notable_work",
        "template_reverse": "What is the most notable work of this person?",
    },
    {
        "prop": "P166",
        "name": "award_received",
        "template_reverse": "Which award did this person receive?",
    },
]

# Wikidata P31 (instance-of) QIDs that mark an entity as country/region/state-
# like — anything here is too broad to be a useful answer.
BANNED_P31_QIDS = {
    "Q6256",       # country
    "Q3624078",    # sovereign state
    "Q3024240",    # historical country
    "Q15180",      # Soviet Union (explicit, very common false-positive)
    "Q1520223",    # former country
    "Q5107",       # continent
    "Q231002",     # nationality
    "Q1763527",    # subregion of Europe
    "Q35657",      # state of the United States (too broad)
    "Q47168",      # federal subject of Russia (oblast / republic; too broad)
}

# String-level safety net (lowercase exact-match labels we always ban).
BANNED_ANSWER_LABELS = {
    "russia", "soviet union", "ussr", "russian empire", "russian federation",
    "united states", "usa", "united states of america", "u.s.", "u.s.a.",
    "germany", "german empire", "weimar republic", "east germany", "west germany",
    "france", "united kingdom", "uk", "great britain", "england", "scotland",
    "china", "people's republic of china", "republic of china", "japan",
    "italy", "spain", "poland", "ukraine", "czechoslovakia",
    "czech republic", "czechia", "hungary", "kazakhstan",
    "europe", "asia", "africa", "americas", "north america", "south america",
    "australia", "antarctica", "oceania",
    "russian sfsr", "ukrainian ssr",
}


def _answer_is_banned(label: str, qid: Optional[str]) -> Tuple[bool, str]:
    """Returns (banned, reason). Cheap label check first, then P31 lookup."""
    if not label:
        return True, "no label"
    lo = label.lower().strip()
    if lo in BANNED_ANSWER_LABELS:
        return True, f"label '{label}' in banned list"
    if qid:
        try:
            p31s = wikidata_get_claim_qids(qid, "P31", max_count=5)
            for pq in p31s:
                if pq in BANNED_P31_QIDS:
                    return True, f"P31 {pq} is country/region-like"
        except Exception:
            pass
    return False, ""


def synth_v2_specific(seed_title: str,
                       batch_answer_counts: Optional[dict] = None,
                       max_repeat_per_answer: int = 3) -> Tuple[Optional[dict], str]:
    """v2 synthesis: tries V2_ANSWER_PROPS in priority order; rejects answers
    that are countries/regions or that already appeared too many times in this
    batch.

    Returns (record, status_string). status_string is "ok" or a reason for
    skipping (no_seed_qid, insufficient_clues, all_props_banned_or_missing).
    Caller is responsible for tracking batch-level answer counts via
    `batch_answer_counts` (a dict mapping label.lower() → count).
    """
    if batch_answer_counts is None:
        batch_answer_counts = {}

    seed_qid = wiki_title_to_qid(seed_title)
    if not seed_qid:
        return None, "no_seed_qid"
    seed_label, _ = wikidata_label_and_enwiki(seed_qid)
    seed_label = seed_label or seed_title

    # Build clues once (v3: each clue is (text, prop_id) for strength scoring).
    # Ban seed name to avoid leakage.
    clue_records = collect_clues_for(seed_qid, banned_labels={seed_label}, max_clues=5)
    if len(clue_records) < 2:
        return None, "insufficient_clues"

    # v3 strength check — reject ambiguous clue sets (e.g. "mathematician + 1910s"
    # alone matches thousands of entities; v2 run had 4/6 failures from this).
    strength_ok, strength_reason = clue_set_strength_ok(clue_records)
    if not strength_ok:
        return None, f"clue_set_too_weak({strength_reason})"

    tried = []
    for spec in V2_ANSWER_PROPS:
        prop = spec["prop"]
        try:
            ans_qid = wikidata_get_claim_qid(seed_qid, prop)
        except Exception as e:
            tried.append(f"{spec['name']}:err({type(e).__name__})")
            continue
        if not ans_qid:
            tried.append(f"{spec['name']}:no_claim")
            continue
        ans_label, _ = wikidata_label_and_enwiki(ans_qid)
        if not ans_label:
            tried.append(f"{spec['name']}:no_label")
            continue
        banned, why = _answer_is_banned(ans_label, ans_qid)
        if banned:
            tried.append(f"{spec['name']}:BANNED({why})")
            continue
        # Frequency cap — don't let one answer dominate a small batch
        if batch_answer_counts.get(ans_label.lower(), 0) >= max_repeat_per_answer:
            tried.append(f"{spec['name']}:freq_cap('{ans_label}'>{max_repeat_per_answer-1})")
            continue
        # Strip any clue that names this specific answer (keep puzzle non-trivial)
        kept_clue_records = [(t, p) for (t, p) in clue_records if ans_label.lower() not in t.lower()]
        if len(kept_clue_records) < 2:
            tried.append(f"{spec['name']}:clues_leak_answer")
            continue
        # After stripping the answer-leaking clue, re-verify strength
        kept_strength_ok, kept_reason = clue_set_strength_ok(kept_clue_records)
        if not kept_strength_ok:
            tried.append(f"{spec['name']}:strength_lost_after_strip({kept_reason})")
            continue
        kept_clue_texts = [t for (t, _) in kept_clue_records]
        clue_block = "; ".join(kept_clue_texts)
        question = (
            f"Identify a person matching all of these clues: {clue_block}. "
            f"{spec['template_reverse']}"
        )
        rec = {
            "id": str(uuid.uuid4()),
            "question": question,
            "answer": ans_label,
            "answer_type": spec["name"],
            "required_entities": [seed_label, ans_label],
            "intermediate_entities": [seed_label],
            "variant": f"v2_specific_{spec['name']}",
            "source_urls": [enwiki_url(seed_title)],
            "n_hops": 2,
            "tag": "synth-wiki-2hop-v2",
            "formalization": [
                ["?SEED", prop, "?Y"],
            ],
            "anchors": {},
            "seed_clues": kept_clue_texts,
            "seed_clue_props": [p for (_, p) in kept_clue_records],
            "clue_strength_summary": kept_reason,
            "wikidata_qids": {
                "seed": seed_qid,
                "answer": ans_qid,
            },
            "v2_tried_chain": tried,  # for debugging which props got skipped
        }
        return rec, "ok"

    return None, "all_props_banned_or_missing: " + "; ".join(tried)


def synth_reverse_union_2hop(seed_title: str) -> Optional[dict]:
    """Construct a Reverse-Union-style 2-hop Q-A: same P19 → P17 chain as
    Basic, but the seed entity's NAME is hidden and replaced with a list of
    discriminating clues. The agent must first deduce who the seed is
    (forcing a search step) before walking to birthplace → country.

    This breaks the "guess wiki/Albert_Einstein" shortcut and produces
    trajectories that include real search + click + read operations.
    """
    seed_qid = wiki_title_to_qid(seed_title)
    if not seed_qid:
        return None
    seed_label, _ = wikidata_label_and_enwiki(seed_qid)
    seed_label = seed_label or seed_title

    birth_qid = wikidata_get_claim_qid(seed_qid, "P19")
    if not birth_qid:
        return None
    birth_label, birth_title = wikidata_label_and_enwiki(birth_qid)
    if not birth_label or not birth_title:
        return None

    country_qid = wikidata_get_claim_qid(birth_qid, "P17", current_only=True)
    if not country_qid:
        return None
    country_label, _ = wikidata_label_and_enwiki(country_qid)
    if not country_label:
        return None
    if country_label.lower() in seed_label.lower():
        return None

    # Build clues, blocking anything that would leak the seed name, the
    # intermediate city, or the answer country.
    banned = {seed_label, birth_label, country_label}
    clues = collect_clue_texts_for(seed_qid, banned, max_clues=4)
    if len(clues) < 2:
        # Need at least 2 distinct clues for the puzzle to be solvable
        # without being trivial — otherwise "a physicist" alone matches
        # thousands of entities.
        return None

    clue_block = "; ".join(clues)
    question = (
        f"Identify a person matching all of these clues: {clue_block}. "
        f"In which country is the birthplace of this person located?"
    )

    record = {
        "id": str(uuid.uuid4()),
        "question": question,
        "answer": country_label,
        "required_entities": [seed_label, birth_label, country_label],
        "intermediate_entities": [birth_label, seed_label],
        "variant": "reverse_union_2hop",
        "source_urls": [
            enwiki_url(seed_title),
            enwiki_url(birth_title),
        ],
        "n_hops": 2,
        "tag": "synth-wiki-2hop-rev",
        "formalization": [
            ["?SEED", "P19", "?M"],   # SEED is now also unknown — must be deduced
            ["?M",    "P17", "?Y"],
        ],
        "anchors": {},                # no anchor revealed
        "seed_clues": clues,           # the puzzle the agent must solve first
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
    ap.add_argument("--seeds", nargs="*", default=None, help="inline seed titles")
    ap.add_argument(
        "--seed-file",
        default=None,
        help="file of seed Wikipedia titles, one per line (lines starting with # are comments)",
    )
    ap.add_argument(
        "--sparql",
        action="store_true",
        help="fetch obscure seeds via Wikidata SPARQL instead of using the default list",
    )
    ap.add_argument(
        "--category",
        action="store_true",
        help="fetch seeds via MediaWiki Category API (SPARQL-free path; works when "
             "WDQS is down or rate-limited). Use --categories to pick which categories.",
    )
    ap.add_argument(
        "--categories",
        nargs="*",
        default=None,
        help="Wikipedia category names to walk (with or without 'Category:' prefix). "
             "If omitted under --category, a curated default list is used.",
    )
    ap.add_argument("--category-max-per-cat", type=int, default=200,
                    help="raw page cap per category before sitelink filtering")
    ap.add_argument("--category-recurse-depth", type=int, default=0,
                    help="recurse into subcategories N levels (0=just the listed category)")
    ap.add_argument(
        "--sparql-occupations",
        nargs="*",
        default=None,
        help="Wikidata QIDs to filter occupation when --sparql (e.g. Q170790=mathematician, Q169470=physicist)",
    )
    ap.add_argument("--sparql-sitelink-min", type=int, default=3,
                    help="lower bound on Wikipedia language editions (fame proxy)")
    ap.add_argument("--sparql-sitelink-max", type=int, default=10,
                    help="upper bound on Wikipedia language editions; smaller = more obscure. "
                         "Default lowered from 25 to 10 after observing that Gemini 3.1 Pro can "
                         "still memorize ~30%% of sitelink<=25 entities.")
    ap.add_argument("--sparql-born-after", type=int, default=1850)
    ap.add_argument("--sparql-born-before", type=int, default=2000)
    ap.add_argument("--sparql-limit", type=int, default=200,
                    help="how many candidate seeds to fetch (we then iterate until --n succeed)")
    ap.add_argument(
        "--mode",
        choices=["basic_2hop", "reverse_union", "specific"],
        default="basic_2hop",
        help="basic_2hop reveals the seed name; reverse_union hides it behind clues. "
             "specific (v2) uses clue-based puzzle AND switches the answer from country "
             "to a specific entity (birthplace city / advisor / employer / educated_at / "
             "notable work / award) with country/region BAN + per-batch frequency cap. "
             "See synthesis_v2_design.md memory for rationale.",
    )
    ap.add_argument(
        "--max-repeat-per-answer",
        type=int,
        default=0,
        help="(specific mode only) Reject candidates whose answer label has already "
             "appeared this many times in the batch. 0 = auto = ceil(N/4).",
    )
    ap.add_argument(
        "--out",
        default=None,
        help="output jsonl path (defaults to data/synth/wiki_2hop_<mode>.jsonl)",
    )
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="seconds between API calls (Wikidata rate-limits aggressively)")
    ap.add_argument(
        "--screen-llm",
        action="store_true",
        help="After each synthesis, ask the agent LLM (no tools) to answer the question. "
             "If it gets it right from memory, DROP the question — useful training data must "
             "force the agent to actually search.",
    )
    ap.add_argument(
        "--screen-aggressive-samples",
        type=int,
        default=3,
        help="(v3) Number of high-temp aggressive samples for the screener's second pass. "
             "Higher = more thorough but slower (default 3, +1 conservative = 4 API calls/Q).",
    )
    ap.add_argument(
        "--screen-llm-tag-only",
        action="store_true",
        help="Like --screen-llm but KEEP the question, just tag llm_guessable=true. "
             "Useful for studying the easy/hard split without recomputing.",
    )
    args = ap.parse_args()

    # ----- assemble seed list -----
    if args.category:
        cats = args.categories or DEFAULT_CATEGORIES
        print(f"[category] walking {len(cats)} categories "
              f"sitelinks=[{args.sparql_sitelink_min},{args.sparql_sitelink_max}] "
              f"recurse_depth={args.category_recurse_depth} target={args.sparql_limit}")
        seeds = fetch_category_seeds(
            categories=cats,
            sitelink_min=args.sparql_sitelink_min,
            sitelink_max=args.sparql_sitelink_max,
            max_per_category=args.category_max_per_cat,
            recurse_depth=args.category_recurse_depth,
            limit=args.sparql_limit,
            sleep=args.sleep,
        )
        print(f"[category] got {len(seeds)} candidate seeds")
    elif args.sparql:
        print(f"[sparql] fetching seeds: occupations={args.sparql_occupations} "
              f"sitelinks=[{args.sparql_sitelink_min},{args.sparql_sitelink_max}] "
              f"born=[{args.sparql_born_after},{args.sparql_born_before}] limit={args.sparql_limit}")
        seeds = fetch_sparql_seeds(
            occupation_qids=args.sparql_occupations,
            sitelink_min=args.sparql_sitelink_min,
            sitelink_max=args.sparql_sitelink_max,
            born_after_year=args.sparql_born_after,
            born_before_year=args.sparql_born_before,
            limit=args.sparql_limit,
        )
        print(f"[sparql] got {len(seeds)} candidate seeds")
    elif args.seed_file:
        seeds = []
        for line in open(args.seed_file):
            s = line.strip()
            if s and not s.startswith("#"):
                seeds.append(s)
        print(f"[seed-file] loaded {len(seeds)} seeds from {args.seed_file}")
    elif args.seeds:
        seeds = args.seeds
    else:
        seeds = DEFAULT_SEEDS

    out_path = args.out or f"data/synth/wiki_2hop_{args.mode}.jsonl"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Auto-tune freq cap from N if user didn't override
    if args.max_repeat_per_answer <= 0:
        import math
        freq_cap = max(2, math.ceil(args.n / 4))
    else:
        freq_cap = args.max_repeat_per_answer

    # Per-batch answer-label counter (used by v2 specific mode)
    batch_answer_counts: dict = {}

    def call_synth(seed_title):
        if args.mode == "basic_2hop":
            return synth_basic_2hop(seed_title), "ok" if True else "miss"
        if args.mode == "reverse_union":
            return synth_reverse_union_2hop(seed_title), "ok"
        if args.mode == "specific":
            return synth_v2_specific(
                seed_title,
                batch_answer_counts=batch_answer_counts,
                max_repeat_per_answer=freq_cap,
            )
        raise ValueError(f"unknown mode {args.mode}")

    written = 0
    skipped = 0
    llm_screened_out = 0
    llm_tagged_guessable = 0
    # v2-specific tallies
    v2_answer_type_counts: dict = {}
    v2_banned_skips = 0
    # v3-specific tallies
    v3_weak_clue_skips = 0
    print(f"[main] mode={args.mode}  n={args.n}  freq_cap={freq_cap}")

    with open(out_path, "w", encoding="utf-8") as f:
        for seed in seeds:
            if written >= args.n:
                break
            try:
                rec, why = call_synth(seed)
            except Exception as e:
                print(f"[!] {seed}: {repr(e)}", file=sys.stderr)
                skipped += 1
                time.sleep(args.sleep)
                continue
            if rec is None:
                # v2 returns a structured "why"; keep skip line informative
                msg = why if why else "synthesis failed (no chain or insufficient clues)"
                print(f"[skip] {seed}: {msg}")
                if args.mode == "specific":
                    if why and why.startswith("all_props_banned"):
                        v2_banned_skips += 1
                    elif why and why.startswith("clue_set_too_weak"):
                        v3_weak_clue_skips += 1
                skipped += 1
                time.sleep(args.sleep)
                continue

            # LLM 预筛(可选)
            if args.screen_llm or args.screen_llm_tag_only:
                guessable, llm_resp = llm_can_answer_without_tools(
                    rec["question"], rec["answer"],
                    aggressive_samples=args.screen_aggressive_samples,
                )
                if guessable:
                    if args.screen_llm:
                        print(
                            f"[screened] {seed}: LLM guessed correctly without tools — "
                            f"answer={rec['answer']!r} llm_said={llm_resp[:60]!r}"
                        )
                        llm_screened_out += 1
                        time.sleep(args.sleep)
                        continue
                    else:
                        # tag-only 模式:保留但标记
                        rec["llm_guessable"] = True
                        rec["llm_guess_response"] = llm_resp[:120]
                        llm_tagged_guessable += 1
                else:
                    if args.screen_llm_tag_only:
                        rec["llm_guessable"] = False
                        rec["llm_guess_response"] = llm_resp[:120]

            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
            # Update per-batch answer counter (for v2 freq cap)
            ans_key = (rec.get("answer") or "").lower()
            if ans_key:
                batch_answer_counts[ans_key] = batch_answer_counts.get(ans_key, 0) + 1
            atype = rec.get("answer_type")
            if atype:
                v2_answer_type_counts[atype] = v2_answer_type_counts.get(atype, 0) + 1
            tag_extra = ""
            if rec.get("llm_guessable") is True:
                tag_extra = "  [LLM-GUESSABLE]"
            atype_tag = f" type={atype}" if atype else ""
            print(
                f"[ok] {seed}: Q='{rec['question'][:100]}' "
                f"A='{rec['answer']}' variant={rec['variant']}{atype_tag}{tag_extra}"
            )
            time.sleep(args.sleep)

    print()
    print(f"wrote {written} Q-A pairs ({skipped} skipped) -> {out_path}")
    if args.screen_llm:
        print(f"  LLM-screen dropped: {llm_screened_out} additional candidates "
              f"(LLM answered correctly without tools)")
    if args.screen_llm_tag_only:
        print(f"  LLM-screen tagged guessable (kept): {llm_tagged_guessable} / {written}")
    if args.mode == "specific":
        print(f"  v3 weak-clue skips (seed had no STRONG/MEDIUM clue): {v3_weak_clue_skips}")
        print(f"  v2 banned-skips (all props blocked): {v2_banned_skips}")
        if v2_answer_type_counts:
            print(f"  v2 answer-type distribution:")
            for t, c in sorted(v2_answer_type_counts.items(), key=lambda kv: -kv[1]):
                print(f"    {t}: {c}")
        if batch_answer_counts:
            print(f"  v2 answer-label distribution (top 10):")
            for lbl, c in sorted(batch_answer_counts.items(), key=lambda kv: -kv[1])[:10]:
                print(f"    {lbl}: {c}")


if __name__ == "__main__":
    main()
