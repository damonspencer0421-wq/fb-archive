#!/usr/bin/env python3
"""Pull public-domain archival photographs from Wikimedia Commons.

Commons top-level categories are usually containers of subcategories with
very few files directly in them, so this walks one level of subcategories
as well. That is the difference between 40 plates and several hundred.
"""
import json, os, re, time, hashlib
import requests

API = "https://commons.wikimedia.org/w/api.php"
UA = "fb-archive-harvester/1.0 (github actions)"
MIN_WIDTH = 1400
MAX_BYTES = 9000000
PER_CATEGORY = 60
SUBCAT_LIMIT = 14
MAX_EDGE = 2400
ROOT = os.path.dirname(os.path.abspath(__file__))
OK_LICENSE = re.compile(r"public domain|^pd|cc0|no known copyright", re.I)
SKIP_SUBCAT = re.compile(r"media needing|categories requiring|to be checked|"
                         r"unidentified|flags of|maps of|coats of arms", re.I)


def api(params):
    p = {"format": "json", "formatversion": "2"}
    p.update(params)
    r = requests.get(API, params=p, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.json()


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
    except Exception as e:
        print("SUBCAT FAIL", cat, e)
        return []
    names = []
    for m in d.get("query", {}).get("categorymembers", []):
        t = m.get("title", "").replace("Category:", "", 1)
        if t and not SKIP_SUBCAT.search(t):
            names.append(t)
    return names[:SUBCAT_LIMIT]


def gather(cat, limit, depth=1):
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
    added = 0

    for page, cats in targets.items():
        outdir = os.path.join(ROOT, "images", page)
        os.makedirs(outdir, exist_ok=True)
        for cat in cats:
            try:
                files = gather(cat, PER_CATEGORY)
            except Exception as e:
                print("SKIP CATEGORY", cat, e)
                continue
            print("CATEGORY", cat, len(files), "candidates")
            kept = 0
            for f in files:
                ii = (f.get("imageinfo") or [{}])[0]
                if ii.get("width", 0) < MIN_WIDTH:
                    continue
                meta = ii.get("extmetadata", {})
                lic = field(meta, "LicenseShortName") + " " + field(meta, "UsageTerms")
                if not OK_LICENSE.search(lic):
                    continue

                url = ii.get("thumburl") or ii.get("url", "")
                if not url:
                    continue
                key = hashlib.sha1(ii.get("url", url).encode()).hexdigest()[:12]
                if key in manifest:
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
                        print("TOO BIG", url, len(r.content))
                        continue
                    with open(dest, "wb") as fh:
                        fh.write(r.content)
                except Exception as e:
                    print("SKIP FILE", url, e)
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
                kept += 1
                time.sleep(0.2)
            print("  kept", kept)

    with open(mpath, "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    print("ADDED", added, "TOTAL", len(manifest))


if __name__ == "__main__":
    main()
