#!/usr/bin/env python3
"""CPG Watch page checker: works from the real RCH page text instead of AI summaries.

  fetch     <list.json> <outdir>                 download each {id,url} with curl (prints BLOCKED if the network refuses rch.org.au)
  text      <outdir>                             HTML -> <id>.txt (main text, tables as "a | b" rows) + <id>.meta.json (sha256, stamp, title, images)
  diff      <old.txt> <new.txt>                  line diff of two page texts (what actually changed on the page)
  snapcheck <snapdir> <textdir> <out.json> [ids] confirm every number in each snapshot against the page text
  quotes    <claims.json> <data.json> <textdir> <out.json>
                                                 build quotes.json for dosecheck.py apply straight from page text (no AI quoting)
  lint      <snapdir> [ids]                      sanity checks on dose strings (unit mismatches, implausible maximums)
  links     <page.html> <out.json>               every link (text, href) on a page, for reading the A-Z index

Run with python3 -I. dosecheck.py must sit in the same folder (its number-matching rules are reused).
"""
import json, os, re, sys, hashlib, difflib, subprocess, html
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
_ns = {"__name__": "dosecheck"}
exec(compile(open(os.path.join(HERE, "dosecheck.py")).read(), "dosecheck.py", "exec"), _ns)
claims_for, match, norm, nums = _ns["claims_for"], _ns["match"], _ns["norm"], _ns["nums"]

# ---------- fetch ----------
def _get(item, outdir):
    path = os.path.join(outdir, item["id"] + ".html")
    try:
        r = subprocess.run(["curl", "-sS", "-L", "--max-time", "40", "--retry", "1", "-A",
                            "Mozilla/5.0 (CPG Watch weekly check)", "-o", path, "-w", "%{http_code} %{url_effective}",
                            item["url"]], capture_output=True, text=True, timeout=90)
        code, _, final = r.stdout.strip().partition(" ")
        return item["id"], {"status": int(code or 0), "finalUrl": final, "error": r.stderr.strip()[:200],
                            "bytes": os.path.getsize(path) if os.path.exists(path) else 0}
    except Exception as e:
        return item["id"], {"status": 0, "finalUrl": "", "error": str(e)[:200], "bytes": 0}

def cmd_fetch(lst, outdir):
    os.makedirs(outdir, exist_ok=True)
    items = json.load(open(lst))
    with ThreadPoolExecutor(6) as ex:
        res = dict(ex.map(lambda it: _get(it, outdir), items))
    json.dump(res, open(os.path.join(outdir, "_fetch.json"), "w"), indent=1)
    ok = [k for k, v in res.items() if v["status"] == 200 and v["bytes"] > 500]
    bad = {k: v for k, v in res.items() if k not in ok}
    print(f"fetched {len(ok)}/{len(items)}")
    for k, v in bad.items(): print("FAILED", k, v["status"], v["error"])
    if items and not ok and any("403" in v["error"] or v["status"] == 403 for v in res.values()):
        print("BLOCKED: the network refused rch.org.au"); sys.exit(2)

# ---------- html -> text ----------
SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form", "iframe", "button", "select", "template"}
SKIP_ATTR = re.compile(r"(^|[\s_-])(nav|navbar|menu|breadcrumbs?|footer|header|sidebar|skip|search|share|social|cookie|banner)([\s_-]|$)", re.I)
BLOCK = {"p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table",
         "thead", "tbody", "tr", "blockquote", "dd", "dt", "dl", "figure", "figcaption", "aside", "pre", "hr"}
VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}
STAMP = re.compile(r"\b(last\s+(?:updated|update|revised|reviewed)|updated)\b\s*:?\s*((?:\d{1,2}\s+)?[A-Za-z]{3,9}\.?,?\s+(?:19|20)\d\d)", re.I)

VISIBLE_PRINT = re.compile(r"(^|\s)visible-print(\s|$)", re.I)
CONTENT_ROOTS = ("rch-primary",)   # RCH pages: the guideline body sits in <div id="rch-primary">
LENIENT_SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "title"}

class Extract(HTMLParser):
    """strict: drops nav/header/footer/forms and menu-like blocks.
    lenient: drops only script/style-type content and relies on the h1 -> stamp slice below
    (used when strict mode loses the guideline body, e.g. a page-wide <form> or unbalanced tags)."""
    def __init__(self, lenient=False, root_id=None):
        super().__init__(convert_charrefs=True)
        self.lenient, self.root_id = lenient, root_id
        self.root_tag, self.root_depth, self.root_seen = None, 0, False
        self.out, self.skip, self.skipname, self.cell, self.images, self.h1, self.in_h1 = [], 0, None, False, [], "", False
        self.doctitle, self.in_title = "", False
    def nl(self): self.out.append("\n")
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title": self.in_title = True
        if self.root_id:
            if self.root_depth:
                if tag == self.root_tag and tag not in VOID: self.root_depth += 1
            elif a.get("id") == self.root_id and not self.root_seen:
                self.root_tag, self.root_depth, self.root_seen = tag, 1, True
                return
            else:
                return
        if self.skip:
            if tag == self.skipname and tag not in VOID: self.skip += 1
            return
        idc = " ".join(filter(None, [a.get("id", ""), a.get("class", ""), a.get("role", "")]))
        if self.lenient:
            drop = tag in LENIENT_SKIP or (tag in ("div", "section") and VISIBLE_PRINT.search(a.get("class", "")))
        else:
            drop = tag in SKIP_TAGS or tag == "title" or (tag in ("div", "section", "ul", "aside") and SKIP_ATTR.search(idc))
        if drop:
            if tag not in VOID: self.skip, self.skipname = 1, tag
            return
        if tag == "img":
            alt = (a.get("alt") or "").strip()
            self.images.append({"alt": alt, "src": a.get("src", "")})
            self.out.append(f" [image: {alt or 'no description'}] ")
        elif tag == "br": self.nl()
        elif tag in ("td", "th"):
            self.out.append(" | " if self.cell else ""); self.cell = True
        elif tag == "tr": self.nl(); self.cell = False
        elif tag in BLOCK: self.nl()
        if tag == "li": self.out.append("- ")
        if tag == "h1": self.in_h1 = True
    def handle_endtag(self, tag):
        if tag == "title": self.in_title = False
        if self.root_id:
            if not self.root_depth: return
            if tag == self.root_tag:
                self.root_depth -= 1
                if not self.root_depth: self.nl(); return
        if self.skip:
            if tag == self.skipname:
                self.skip -= 1
                if not self.skip: self.skipname = None
            return
        if tag == "h1": self.in_h1 = False
        if tag in BLOCK or tag == "tr": self.nl()
    def handle_data(self, d):
        if self.in_title: self.doctitle += d
        if self.root_id and not self.root_depth: return
        if self.skip: return
        self.out.append(d)
        if self.in_h1: self.h1 += d

def _extract(raw, lenient, root_id=None):
    p = Extract(lenient, root_id); p.feed(raw); p.close()
    lines = []
    for ln in "".join(p.out).replace("\xa0", " ").split("\n"):
        ln = re.sub(r"[ \t\r\f\v]+", " ", ln).strip(" |")
        ln = re.sub(r"\s+\|\s+", " | ", ln).strip()
        if ln and not re.fullmatch(r"-?", ln): lines.append(ln)
    title = re.sub(r"\s+", " ", p.h1).strip()
    if not title:  # fall back to the <title> element, e.g. "Clinical Practice Guidelines : Anaphylaxis"
        dt = re.sub(r"\s+", " ", p.doctitle).strip()
        title = dt.split(" : ", 1)[1].strip() if " : " in dt else dt
    start = next((i for i, l in enumerate(lines) if title and l == title), 0)
    stamp_i = [i for i, l in enumerate(lines) if STAMP.search(l)]
    end = stamp_i[-1] + 1 if stamp_i and stamp_i[-1] >= start else len(lines)
    body = lines[start:end]
    m = STAMP.search(lines[stamp_i[-1]]) if stamp_i and stamp_i[-1] >= start else None
    stamp = re.sub(r"\s+", " ", m.group(0)).strip() if m else ""
    text = "\n".join(body) + "\n"
    return text, {"title": title, "stamp": stamp, "images": p.images, "imageCount": len(p.images),
                  "lines": len(body), "mode": ("root:" + root_id) if root_id else ("lenient" if lenient else "strict"),
                  "h1": bool(p.h1.strip())}

def html_to_text(raw):
    """RCH pages: read only the content block (<div id="rch-primary">), dropping script/style and the
    print-only header, then cut from the <h1> to the "Last updated" line.
    Other pages: strict extraction; if it loses the page (no <h1> or under 300 characters), lenient.
    The choice depends only on the HTML, so hashes stay comparable week to week."""
    for rid in CONTENT_ROOTS:
        if re.search(r'id\s*=\s*["\']%s["\']' % re.escape(rid), raw):
            text, meta = _extract(raw, True, rid)
            if meta["h1"] and len(text) >= 200: return text, meta
    text, meta = _extract(raw, False)
    if not meta["h1"] or len(text) < 300:
        t2, m2 = _extract(raw, True)
        if len(t2) > len(text): text, meta = t2, m2
    return text, meta

def cmd_text(outdir):
    res = {}
    for f in sorted(os.listdir(outdir)):
        if not f.endswith(".html"): continue
        gid = f[:-5]
        raw = open(os.path.join(outdir, f), encoding="utf-8", errors="replace").read()
        text, meta = html_to_text(raw)
        meta["sha256"] = hashlib.sha256(text.encode()).hexdigest()
        meta["chars"] = len(text)
        open(os.path.join(outdir, gid + ".txt"), "w").write(text)
        json.dump(meta, open(os.path.join(outdir, gid + ".meta.json"), "w"), ensure_ascii=False, indent=1)
        res[gid] = {k: meta[k] for k in ("sha256", "stamp", "title", "chars", "imageCount")}
        if meta["chars"] < 300: print("SHORT", gid, meta["chars"], "chars - check extraction")
    json.dump(res, open(os.path.join(outdir, "_text.json"), "w"), ensure_ascii=False, indent=1)
    print(f"text extracted for {len(res)} pages")

# ---------- diff ----------
def cmd_diff(a, b):
    A = open(a).read().splitlines(); B = open(b).read().splitlines()
    for ln in difflib.unified_diff(A, B, "previous", "current", n=1, lineterm=""): print(ln)

# ---------- number confirmation against page text ----------
def find_quote(claim, lines):
    for i, ln in enumerate(lines):
        if match(claim, ln): return ln
    for i in range(len(lines) - 1):   # values split across adjacent lines/cells
        j = lines[i] + " " + lines[i + 1]
        if match(claim, j): return j
    return None

SNAP_FIELDS = ["keyPoints", "redFlags", "thresholds", "doses", "investigations", "imaging", "management"]
def snap_texts(s):
    out = []
    for k in SNAP_FIELDS:
        v = s.get(k) or []
        out += v if isinstance(v, list) else [v]
    for k, v in (s.get("disposition") or {}).items(): out += v or []
    if s.get("scope"): out.append(s["scope"] if isinstance(s["scope"], str) else json.dumps(s["scope"]))
    return [t for t in out if isinstance(t, str)]

def cmd_snapcheck(snapdir, textdir, out, ids):
    res = {}
    for f in sorted(os.listdir(snapdir)):
        gid = f[:-5]
        if not f.endswith(".json") or (ids and gid not in ids): continue
        tp = os.path.join(textdir, gid + ".txt")
        if not os.path.exists(tp): continue
        s = json.load(open(os.path.join(snapdir, f)))
        lines = open(tp).read().splitlines()
        cl = [c for c in claims_for({"points": snap_texts(s)}) if not c["prev"]]
        missing = [{"claim": c["claim"], "context": c["context"]} for c in cl if not find_quote(c, lines)]
        res[gid] = {"checked": len(cl), "confirmed": len(cl) - len(missing), "missing": missing}
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    bad = {k: v for k, v in res.items() if v["missing"]}
    print(f"snapshots checked {len(res)}; all numbers confirmed in {len(res)-len(bad)}; {len(bad)} with unconfirmed numbers")
    for k, v in bad.items(): print(" ", k, f"{v['confirmed']}/{v['checked']}", [m["claim"] for m in v["missing"]])

def cmd_quotes(claims_p, data_p, textdir, out):
    C = json.load(open(claims_p)); D = json.load(open(data_p))
    gl = {d["id"]: d["guideline"] for d in D["digests"]}
    res = {}
    for did, v in C.items():
        tp = os.path.join(textdir, gl.get(did, "") + ".txt")
        if not os.path.exists(tp): print("NO PAGE TEXT for", did, "- use the WebFetch route"); continue
        lines = open(tp).read().splitlines()
        res[did] = {c["key"]: (find_quote(c, lines) or "NOT FOUND") for c in v["claims"] if not c["prev"]}
    json.dump(res, open(out, "w"), ensure_ascii=False, indent=1)
    print("quotes written for", len(res), "digests")

# ---------- lint ----------
MASS = {"g": 1000.0, "mg": 1.0, "microg": 0.001, "mcg": 0.001, "micrograms": 0.001}
_M = r"(?:mg|microg|mcg|micrograms|g)"
PERKG = re.compile(r"(\d+(?:\.\d+)?)\s*(mg|microg|mcg|micrograms|g)/kg(?:/dose|/day)?"
                   r"(?:(?!;|\. |\d+(?:\.\d+)?\s*" + _M + r"\b).){0,45}?"
                   r"(?:max(?:imum)?\.?:?\s*|\(|up to\s+)(\d+(?:\.\d+)?)\s*(mg|microg|mcg|micrograms|g)\b(?!\s*/)", re.I)
def lint_string(t):
    issues = []
    vol = re.search(r"(\d+(?:\.\d+)?)\s*m[lL]/kg", t)
    mxm = re.search(r"max(?:imum)?\.?\s*(\d+(?:\.\d+)?)\s*(mg|g|microg)\b(?!\s*/)", t, re.I)
    if vol and mxm and not re.search(r"(mg|microg|mcg|g|mmol)/kg|mg/m[lL]|=", t, re.I):
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
        mx = float(mxm.group(1)) * MASS[mxm.group(2).lower()]
        if not pct:
            issues.append("volume per kg (mL/kg) with a mass maximum and no concentration or mg/kg dose")
        elif mx < float(vol.group(1)) * float(pct.group(1)) * 10 * 3:
            issues.append(f"{vol.group(1)} mL/kg of {pct.group(1)}% is {float(vol.group(1))*float(pct.group(1))*10:g} mg/kg, but the maximum is {mxm.group(1)} {mxm.group(2)}")
    for m in PERKG.finditer(t):
        dose = float(m.group(1)) * MASS[m.group(2).lower()]
        mx = float(m.group(3)) * MASS[m.group(4).lower()]
        if mx < dose * 3: issues.append(f"maximum {m.group(3)} {m.group(4)} is less than the dose for a 3 kg infant ({m.group(1)} {m.group(2)}/kg)")
    for m in re.finditer(r"\bmax(?:imum)?\.?\s*(\d+(?:\.\d+)?)\s*g\b", t, re.I):
        if float(m.group(1)) >= 50: issues.append(f"maximum of {m.group(1)} g looks implausible (mg?)")
    return issues

def cmd_lint(snapdir, ids):
    n = 0
    for f in sorted(os.listdir(snapdir)):
        gid = f[:-5]
        if not f.endswith(".json") or (ids and gid not in ids): continue
        s = json.load(open(os.path.join(snapdir, f)))
        for t in snap_texts(s):
            for i in lint_string(t):
                n += 1; print(f"{gid}: {i}\n    {t[:220]}")
    print(f"lint: {n} issue(s)")

# ---------- index links ----------
class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.links, self.cur = [], None
    def handle_starttag(self, tag, attrs):
        if tag == "a": self.cur = [dict(attrs).get("href", ""), ""]
    def handle_data(self, d):
        if self.cur is not None: self.cur[1] += d
    def handle_endtag(self, tag):
        if tag == "a" and self.cur is not None:
            href, txt = self.cur; self.cur = None
            txt = re.sub(r"\s+", " ", txt).strip()
            if txt: self.links.append({"text": txt, "href": href})

def cmd_links(html_p, out):
    p = Links(); p.feed(open(html_p, encoding="utf-8", errors="replace").read()); p.close()
    json.dump(p.links, open(out, "w"), ensure_ascii=False, indent=1)
    gi = [l for l in p.links if "guideline_index" in l["href"].lower() or l["href"].lower().endswith(".pdf")]
    print(f"{len(p.links)} links, {len(gi)} pointing at guideline pages or PDFs")

if __name__ == "__main__":
    a = sys.argv
    {"fetch": lambda: cmd_fetch(a[2], a[3]), "text": lambda: cmd_text(a[2]), "diff": lambda: cmd_diff(a[2], a[3]),
     "snapcheck": lambda: cmd_snapcheck(a[2], a[3], a[4], a[5:]), "quotes": lambda: cmd_quotes(a[2], a[3], a[4], a[5]),
     "lint": lambda: cmd_lint(a[2], a[3:]), "links": lambda: cmd_links(a[2], a[3])}[a[1]]()
