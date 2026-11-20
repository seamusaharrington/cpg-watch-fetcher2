#!/usr/bin/env python3
"""CPG Watch fetcher: runs on a GitHub Actions runner (open internet) and saves the
full text of every RCH Clinical Practice Guideline page for the weekly CPG Watch check.

  python3 fetch_rch.py [outdir]          (default outdir: latest)

Writes <outdir>/bundle.json:
  {generatedAt, source, index:{text, sha256, links:[{text,href}], newUpdatedBox},
   pages:{<url>:{status, finalUrl, error, title, stamp, sha256, chars, imageCount, images, text}},
   counts:{...}}
The text is produced by tools/pagecheck.py's html_to_text, the same extraction the tracker
uses, so hashes are comparable from run to run. Standard library only.
"""
import json, os, re, sys, time, hashlib, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
_ns = {"__name__": "pagecheck", "__file__": os.path.join(HERE, "tools", "pagecheck.py")}
exec(compile(open(_ns["__file__"]).read(), "pagecheck.py", "exec"), _ns)
html_to_text, Links = _ns["html_to_text"], _ns["Links"]

INDEX = "https://www.rch.org.au/clinicalguide/"
UA = "Mozilla/5.0 (CPG Watch weekly check; GitHub Actions)"
SKIP_TITLES = {"cpg committee calendar", "cpg information", "other resources", "paediatric improvement collaborative",
               "parent resources", "retrieval services", "adverse drug reaction (adr) reporting",
               "okee in medical imaging app", "x - complete an online death certificate"}

def norm(u): return u.strip().lower().rstrip("/")

def get(url, tries=3):
    last = {}
    for i in range(tries):
        try:
            req = urllib.request.Request(url.replace(" ", "%20"), headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                raw = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
                return {"status": r.status, "finalUrl": r.geturl(), "error": "", "html": raw}
        except urllib.error.HTTPError as e:
            last = {"status": e.code, "finalUrl": url, "error": str(e)[:200], "html": ""}
            if e.code in (404, 410): break
        except Exception as e:
            last = {"status": 0, "finalUrl": url, "error": str(e)[:200], "html": ""}
        time.sleep(3 * (i + 1))
    return last

def absolute(href):
    if href.startswith("http"): return href
    if href.startswith("//"): return "https:" + href
    if href.startswith("/"): return "https://www.rch.org.au" + href
    return INDEX + href

def main(outdir):
    os.makedirs(outdir, exist_ok=True)
    idx = get(INDEX)
    if idx.get("status") != 200:
        print("INDEX FAILED", idx.get("status"), idx.get("error")); sys.exit(1)
    itext, imeta = html_to_text(idx["html"])
    p = Links(); p.feed(idx["html"]); p.close()
    links, seen, targets = [], set(), []
    for l in p.links:
        h = l["href"]
        if not ("guideline_index" in h.lower() or h.lower().endswith(".pdf")): continue
        if l["text"].strip().lower() in SKIP_TITLES: continue
        u = absolute(h)
        links.append({"text": l["text"], "href": u})
        if norm(u) in seen: continue
        seen.add(norm(u))
        if not u.lower().endswith(".pdf"): targets.append(u)
    extra = os.path.join(HERE, "extra_urls.txt")
    if os.path.exists(extra):
        for u in open(extra).read().split():
            if norm(u) not in seen: seen.add(norm(u)); targets.append(u)
    m = re.search(r"New and updated clinical practice guidelines(.{0,1500})", itext, re.S | re.I)
    box = m.group(1).strip() if m else ""

    def one(u):
        r = get(u); time.sleep(0.5)
        rec = {k: r.get(k, "") for k in ("status", "finalUrl", "error")}
        if r.get("html"):
            text, meta = html_to_text(r["html"])
            rec.update({"title": meta["title"], "stamp": meta["stamp"], "imageCount": meta["imageCount"],
                        "images": meta["images"], "chars": len(text),
                        "sha256": hashlib.sha256(text.encode()).hexdigest(), "text": text})
        return u, rec
    with ThreadPoolExecutor(4) as ex:
        pages = dict(ex.map(one, targets))
    ok = sum(1 for v in pages.values() if v["status"] == 200 and v.get("chars", 0) > 0)
    bundle = {"generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": INDEX,
              "index": {"text": itext, "sha256": hashlib.sha256(itext.encode()).hexdigest(), "links": links,
                        "newUpdatedBox": box},
              "pages": pages,
              "counts": {"indexLinks": len(links), "distinctUrls": len(seen), "fetched": len(targets), "ok": ok}}
    json.dump(bundle, open(os.path.join(outdir, "bundle.json"), "w"), ensure_ascii=False, indent=0)
    print(json.dumps(bundle["counts"]))
    for u, v in pages.items():
        if v["status"] != 200: print("FAILED", v["status"], u, v["error"])
        elif v.get("chars", 0) < 300: print("SHORT", v.get("chars"), u)

if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "latest"))
