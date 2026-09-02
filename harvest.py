#!/usr/bin/env python3
"""Pull public-domain archival photographs from Wikimedia Commons.

Writes report.json alongside manifest.json so the yield of every category
is visible without reading the Action log. A category that does not exist
shows exists=false, which is the difference between a bad guess and a
genuinely empty vein.
"""
import json, os, re, time, hashlib
import requests

API = "https://commons.wikimedia.org/w/api.php"
UA = "fb-archive-harvester/1.0 (github actions)"
MIN_WIDTH = 1100
MAX_BYTES = 9000000
PER_CATEGORY = 60
SUBCAT_LIMIT = 14
MAX_EDGE = 2400
ROOT = os.path.dirname(os.path.abspath(__file__))
OK_LICENSE = re.compile(r"public domain|^pd|cc0|no known copyright|cc.by", re.I)
SKIP_SUBCAT = re.compile(r"media needing|categories requiring|to be checked|"
                         r"unidentified|flags of|maps of|coats of arms", re.I)


def api(params):
    p = {"format": "json", "formatversion": "2"}
    p.update(params)
    r = requests.get(API, params=p, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.json()


def cat_exists(cat):
    try:
        d = api({"action": "query", "titles": "Category:" + cat})
        pages = d.get("query", {}).get("pages", [])
        return bool(pages) and not pages[0].get("missing", False)
    except Exception:
        return None


def direct_files(cat, limit):
    out, cont = [], {}
    while len(out) < limit:
        q = {
            "action": "query",
            "generator": "categorymembers",
            "gcmtitle": "Category:" + cat,
            "gcmtype": "file",
            "gcmlimit": "50",
            "prop": "imageinfo",
            "iiprop": "url|size|extmetadata",
            "iiurlwidth": str(MAX_EDGE),
        }
        q.update(cont)
        d = api(q)
        out.extend(d.get("query", {}).get("pages", []))
        if "continue" not in d:
            break
        cont = d["continue"]
        time.sleep(0.3)
    return out[:limit]


def subcats(cat):
    try:
        d = api({
            "action": "query",
            "list": "categorymembers",
            "cmtitle": "Category:" + cat,
            "cmtype": "subcat",
            "cmlimit": "50",
        })
    except Exception:
        return []
    names = []
    for m in d.get("query", {}).get("categorymembers", []):
        t = m.get("title", "").replace("Category:", "", 1)
        if t and not SKIP_SUBCAT.search(t):
            names.append(t)
    return names[:SUBCAT_LIMIT]


def gather(cat, limit, depth=2):
    files = direct_files(cat, limit)
    if len(files) < limit and depth > 0:
        for sub in subcats(cat):
            if len(files) >= limit:
                break
            files.extend(gather(sub, limit - len(files), depth - 1))
    return files[:limit]


def field(meta, key):
    v = (meta or {}).get(key, {}).get("value", "")
    return re.sub(r"<[^>]+>", "", str(v)).strip()[:400]


def main():
    targets = json.load(open(os.path.join(ROOT, "targets.json")))
    mpath = os.path.join(ROOT, "manifest.json")
    manifest = json.load(open(mpath)) if os.path.exists(mpath) else {}
    report = {}
    added = 0

    for page, cats in targets.items():
        outdir = os.path.join(ROOT, "images", page)
        os.makedirs(outdir, exist_ok=True)
        for cat in cats:
            rec = {"page": page, "exists": cat_exists(cat),
                   "candidates": 0, "too_small": 0, "wrong_license": 0,
                   "already_had": 0, "kept": 0}
            report[cat] = rec
            if rec["exists"] is False:
                print("MISSING CATEGORY", cat)
                continue
            try:
                files = gather(cat, PER_CATEGORY)
            except Exception as e:
                rec["error"] = str(e)[:200]
                continue
            rec["candidates"] = len(files)

            for f in files:
                ii = (f.get("imageinfo") or [{}])[0]
                if ii.get("width", 0) < MIN_WIDTH:
                    rec["too_small"] += 1
                    continue
                meta = ii.get("extmetadata", {})
                lic = field(meta, "LicenseShortName") + " " + field(meta, "UsageTerms")
                if not OK_LICENSE.search(lic):
                    rec["wrong_license"] += 1
                    continue

                url = ii.get("thumburl") or ii.get("url", "")
                if not url:
                    continue
                key = hashlib.sha1(ii.get("url", url).encode()).hexdigest()[:12]
                if key in manifest:
                    rec["already_had"] += 1
                    continue

                ext = os.path.splitext(url.split("?")[0])[1].lower()
                if ext not in (".jpg", ".jpeg", ".png"):
                    ext = ".jpg"
                name = key + ext
                dest = os.path.join(outdir, name)
                try:
                    r = requests.get(url, headers={"User-Agent": UA}, timeout=120)
                    r.raise_for_status()
                    if len(r.content) > MAX_BYTES:
                        continue
                    with open(dest, "wb") as fh:
                        fh.write(r.content)
                except Exception:
                    continue

                manifest[key] = {
                    "page": page,
                    "category": cat,
                    "path": "images/%s/%s" % (page, name),
                    "title": f.get("title", ""),
                    "orig_width": ii.get("width"),
                    "orig_height": ii.get("height"),
                    "credit": field(meta, "Credit") or field(meta, "Artist"),
                    "description": field(meta, "ImageDescription"),
                    "date": field(meta, "DateTimeOriginal"),
                    "license": field(meta, "LicenseShortName"),
                    "source": ii.get("descriptionurl", ""),
                }
                added += 1
                rec["kept"] += 1
                time.sleep(0.2)
            print(cat, rec)

    with open(mpath, "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    with open(os.path.join(ROOT, "report.json"), "w") as fh:
        json.dump(report, fh, indent=1, sort_keys=True)
    print("ADDED", added, "TOTAL", len(manifest))


if __name__ == "__main__":
    main()
