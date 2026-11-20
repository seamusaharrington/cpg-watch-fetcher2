#!/usr/bin/env python3
"""CPG Watch dose checker.

Confirms that every number the tracker wrote (doses, maximums, thresholds,
ages, durations) appears on the live RCH guideline page.

  extract  <data.json> <out.json> [ids...]   build the claim list and a WebFetch prompt per digest
  apply    <data.json> <claims.json> <quotes.json> <date>   score WebFetch quotes, write digest.verify

quotes.json maps digest id -> {claim_key: "exact quote from page" | "NOT FOUND"}.
Values the summary marks as previous ("was ...", "previously ...") are checked
against the digest's own change record instead of the live page.
"""
import json, re, sys

UNIT = (r"mg/kg/dose|mg/kg/day|mg/kg|microg/kg|mcg/kg|mmol/kg|mL/kg|ml/kg|g/kg|"
        r"mL/min/1\.73m2|mg/mmol|micromol/L|mmol/L|g/L|g/dL|mmHg|"
        r"microg|mcg|mmol|mg|mL|ml|g\b|kg|L\b|%|°C?|"
        r"hours?|hourly|h\b|H\b|days?|weeks?|months?|years?|minutes?|min\b|mo\b|y\b")
NUM = r"\d+(?:\.\d+)?"
CLAIM = re.compile(
    rf"(?:[<>≤≥]=?\s*)?{NUM}(?:\s*(?:[-–]|to)\s*{NUM})?(?:\s*(?:{UNIT}))?",
    re.I)
PREV = re.compile(
    r"\((?:was|previously)[^)]*\)"                      # (was 10 mg/kg)
    r"|\b(?:previously|previous|was|formerly)\b(?:(?!\.\s|\.$|;).)*"  # to the end of the sentence
    r"|\bnot\s+(?=[<>≤≥]?\d)(?:(?!\.\s|\.$|;|,).)*",      # not 28 days
    re.I)
YEAR = re.compile(r"^(19|20)\d\d$")
STAMP = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\.?,?\s+(19|20)\d\d", re.I)

def norm(s):
    s = s.replace("–", "-").replace("—", "-").replace(" ", " ").replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s)
    s = s.replace("µmol", "micromol").replace("μmol", "micromol").replace("µg", "microg").replace("μg", "microg")
    s = re.sub(r"\s*°\s*c?\b", " degrees ", s, flags=re.I).replace("°", " degrees ")
    s = re.sub(r"(\d)\s+to\s+(\d)", r"\1-\2", s)
    return re.sub(r"\s+", " ", s).lower().replace("mcg", "microg")

def nums(s):
    return re.findall(NUM, s)

def unit_of(tok):
    m = re.search(rf"({UNIT})\s*$", tok, re.I)
    return m.group(1).lower() if m else ""

def texts(d):
    b = d.get("bulletin") or {}
    out = [("bulletin", b.get("summary", ""))] + [("bulletin", a) for a in b.get("actions", [])]
    if b.get("doseAlert"): out.append(("bulletin", b["doseAlert"]))
    out += [("points", p) for p in d.get("points", [])] + [("headline", d.get("headline", ""))]
    return out

def claims_for(d):
    seen, out = set(), []
    for src, t in texts(d):
        t2 = STAMP.sub(" ", t)
        prev_spans = [m.span() for m in PREV.finditer(t2)]
        for m in CLAIM.finditer(t2):
            tok = m.group(0).strip()
            ns = nums(tok)
            if not ns or (len(ns) == 1 and YEAR.match(ns[0])): continue
            u = unit_of(tok)
            # bare single digits with no unit are list counts, not clinical values
            if not u and len(ns) == 1 and len(ns[0]) == 1 and not re.match(r"[<>≤≥]", tok): continue
            prev = any(a <= m.start() < b for a, b in prev_spans)
            key = norm(tok) + ("|prev" if prev else "")
            if key in seen: continue
            seen.add(key)
            ctx = t[max(0, m.start() - 60): m.end() + 40].strip()
            out.append({"key": key, "claim": tok, "unit": u, "prev": prev, "src": src, "context": ctx})
    return out

def prompt_for(cl):
    live = [c for c in cl if not c["prev"]]
    lines = "\n".join(f'{i+1}. "{c["claim"]}" (context: …{c["context"]}…)' for i, c in enumerate(live))
    return ("You are checking a clinical summary against this guideline page. For each numbered value below, "
            "find where that exact value (same numbers and unit) appears on the page and copy the sentence or table cell "
            "containing it WORD FOR WORD, unchanged. If the value does not appear anywhere on the page, write NOT FOUND. "
            "Do not paraphrase, round or correct anything. Reply only with lines in the form '<number>. <quote or NOT FOUND>'.\n\n"
            + lines)

def match(claim, quote):
    if not quote or "not found" in quote.lower(): return False
    q = norm(quote)
    ns = nums(claim["claim"])
    pos = 0
    for n in ns:
        m = re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", q[pos:])
        if not m: return False
        pos += m.end()
    u = claim["unit"]
    if u and u not in ("h", "y", "mo", "hourly", "hours", "hour", "days", "day", "weeks", "week", "months", "month", "years", "year", "minutes", "minute", "min"):
        nu = norm(u).strip()
        if nu not in q and nu.replace(" ", "") not in q.replace(" ", ""): return False
    return True

def parse_quotes(text, live):
    res = {}
    for line in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.*)", line)
        if m and 1 <= int(m.group(1)) <= len(live):
            res[live[int(m.group(1)) - 1]["key"]] = m.group(2).strip()
    return res

def cmd_extract(data, out, ids):
    D = json.load(open(data))
    res = {}
    for d in D["digests"]:
        if ids and d["id"] not in ids: continue
        cl = claims_for(d)
        res[d["id"]] = {"url": d["url"], "claims": cl, "prompt": prompt_for(cl)}
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    for k, v in res.items():
        print(k, len(v["claims"]), "claims", sum(1 for c in v["claims"] if c["prev"]), "previous")

def cmd_apply(data, claims_p, quotes_p, date):
    D = json.load(open(data)); C = json.load(open(claims_p)); Q = json.load(open(quotes_p))
    for d in D["digests"]:
        if d["id"] not in C or d["id"] not in Q: continue
        cl = C[d["id"]]["claims"]; live = [c for c in cl if not c["prev"]]
        raw = Q[d["id"]]
        quotes = parse_quotes(raw, live) if isinstance(raw, str) else raw
        hist = norm(" ".join(c["text"] for c in d.get("changes", [])) + " " + d.get("changeNote", ""))
        missing, ok = [], 0
        for c in cl:
            if c["prev"]:
                good = all(re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", hist) for n in nums(c["claim"]))
            else:
                good = match(c, quotes.get(c["key"], ""))
            if good: ok += 1
            else: missing.append({"claim": c["claim"], "context": c["context"], "where": c["src"],
                                  "kind": "previous value" if c["prev"] else "current value",
                                  "found": (quotes.get(c["key"], "") if not c["prev"] else "")[:200]})
        status = "none" if not cl else ("verified" if not missing else "unverified")
        d["verify"] = {"date": date, "status": status, "checked": len(cl), "confirmed": ok, "missing": missing}
        print(d["id"], status, f"{ok}/{len(cl)}", [m["claim"] for m in missing])
    s = json.dumps(D, ensure_ascii=False, separators=(",", ":"))
    open(data, "w").write(s)

if __name__ == "__main__":
    a = sys.argv
    if a[1] == "extract": cmd_extract(a[2], a[3], a[4:])
    elif a[1] == "apply": cmd_apply(a[2], a[3], a[4], a[5])
