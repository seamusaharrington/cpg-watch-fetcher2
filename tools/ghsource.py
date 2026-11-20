#!/usr/bin/env python3
"""CPG Watch: load the page text that the GitHub fetcher saved, in the same layout as
"pagecheck.py fetch" + "pagecheck.py text", so every other pagecheck command works unchanged.

  pull <owner/repo> <data.json> <pagesdir> [maxAgeHours=72]

Reads latest/bundle.json with an anonymous shallow git clone of the (public) repo; if that fails,
falls back to api.github.com (raw media type).
Writes into <pagesdir>:
  <id>.txt, <id>.meta.json, _text.json, _fetch.json    for every tracker guideline found in the bundle
  _index.txt, _index.links.json, _bundle_info.json     the CPG index text, its guideline links, run info
Prints NEW (index URL not in the tracker), GONE (tracker URL no longer on the index) and MISSING lines.
Exit codes: 0 ok, 2 bundle unavailable (use the next route; also used when the bundle has no usable
page text, i.e. most pages under 300 characters because the fetcher's extraction failed), 3 bundle older
than maxAgeHours.
Run with python3 -I.
"""
import json, os, shutil, subprocess, sys
from datetime import datetime, timezone

def norm(u): return u.strip().lower().replace(" ", "%20").rstrip("/")

def load_bundle(repo, outdir):
    notes = []
    clone = os.path.join(outdir, "_repo")
    shutil.rmtree(clone, ignore_errors=True)
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1", GIT_TERMINAL_PROMPT="0")
    try:
        r = subprocess.run(["git", "clone", "--depth", "1", "-q", f"https://github.com/{repo}", clone],
                           capture_output=True, text=True, env=env, timeout=600)
        B = json.load(open(os.path.join(clone, "latest", "bundle.json")))
        assert "pages" in B
        return B, "git clone"
    except Exception as e:
        notes.append("git clone failed: " + str(e)[:200])
    finally:
        shutil.rmtree(clone, ignore_errors=True)
    raw_p = os.path.join(outdir, "_bundle.json")
    r = subprocess.run(["curl", "-sS", "-L", "--max-time", "120", "-H", "Accept: application/vnd.github.raw+json",
                        "-H", "X-GitHub-Api-Version: 2022-11-28", "-o", raw_p, "-w", "%{http_code}",
                        f"https://api.github.com/repos/{repo}/contents/latest/bundle.json"],
                       capture_output=True, text=True)
    try:
        B = json.load(open(raw_p))
        assert "pages" in B
        return B, "GitHub API"
    except Exception:
        msg = open(raw_p).read(400) if os.path.exists(raw_p) else ""
        print("UNAVAILABLE", "; ".join(notes), "| API", r.stdout.strip(), r.stderr.strip()[:200], msg.replace("\n", " "))
        sys.exit(2)
    finally:
        if os.path.exists(raw_p): os.remove(raw_p)

def pull(repo, data_p, outdir, max_age=72):
    os.makedirs(outdir, exist_ok=True)
    B, via = load_bundle(repo, outdir)
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(B["generatedAt"])).total_seconds() / 3600
    D = json.load(open(data_p))
    pages = {norm(u): v for u, v in B["pages"].items()}
    fetch, textj, missing = {}, {}, []
    for g in D["guidelines"]:
        if g["url"].lower().endswith(".pdf"): continue
        v = pages.get(norm(g["url"]))
        if v is None: missing.append(g["id"]); continue
        fetch[g["id"]] = {"status": v.get("status", 0), "finalUrl": v.get("finalUrl", ""), "error": v.get("error", ""),
                          "bytes": v.get("chars", 0)}
        if "text" not in v: continue
        open(os.path.join(outdir, g["id"] + ".txt"), "w").write(v["text"])
        meta = {k: v.get(k) for k in ("title", "stamp", "images", "imageCount", "sha256", "chars")}
        meta["lines"] = v["text"].count("\n")
        json.dump(meta, open(os.path.join(outdir, g["id"] + ".meta.json"), "w"), ensure_ascii=False, indent=1)
        textj[g["id"]] = {k: meta[k] for k in ("sha256", "stamp", "title", "chars", "imageCount")}
    json.dump(fetch, open(os.path.join(outdir, "_fetch.json"), "w"), indent=1)
    json.dump(textj, open(os.path.join(outdir, "_text.json"), "w"), ensure_ascii=False, indent=1)
    open(os.path.join(outdir, "_index.txt"), "w").write(B["index"]["text"])
    json.dump(B["index"]["links"], open(os.path.join(outdir, "_index.links.json"), "w"), ensure_ascii=False, indent=1)
    info = {"generatedAt": B["generatedAt"], "ageHours": round(age, 1), "counts": B.get("counts", {}), "via": via,
            "newUpdatedBox": B["index"].get("newUpdatedBox", ""), "indexSha256": B["index"].get("sha256", "")}
    json.dump(info, open(os.path.join(outdir, "_bundle_info.json"), "w"), ensure_ascii=False, indent=1)
    tracked = {norm(g["url"]) for g in D["guidelines"]}
    linked = {}
    for l in B["index"]["links"]: linked.setdefault(norm(l["href"]), l)
    print(f"bundle {B['generatedAt']} ({age:.0f} h old, via {via}); index URLs {len(linked)}; page texts written {len(textj)}")
    for u, l in linked.items():
        if u not in tracked: print("NEW", l["text"], "|", l["href"])
    for g in D["guidelines"]:
        if g.get("status") != "retired" and norm(g["url"]) not in linked: print("GONE", g["id"], "|", g["url"])
    for i in missing: print("MISSING", i)
    short = 0
    for i, f in fetch.items():
        if f["status"] != 200: print("FAILED", i, f["status"], f["error"])
        elif textj.get(i, {}).get("chars", 0) < 300: short += 1; print("SHORT", i, textj.get(i, {}).get("chars"))
    if age > max_age: print(f"STALE: bundle is {age:.0f} hours old"); sys.exit(3)
    if textj and short > len(textj) / 2:
        print(f"UNAVAILABLE: no usable page text ({short} of {len(textj)} pages are under 300 characters; the fetcher's extraction failed)")
        sys.exit(2)

if __name__ == "__main__":
    a = sys.argv
    if len(a) >= 5 and a[1] == "pull": pull(a[2], a[3], a[4], float(a[5]) if len(a) > 5 else 72)
    else: print(__doc__); sys.exit(1)
