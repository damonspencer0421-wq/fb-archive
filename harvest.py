#!/usr/bin/env python3
"""Pull public-domain archival photographs from Wikimedia Commons.

Wikimedia rate-limits hard from cloud IPs. Three things keep it happy:
a User-Agent that identifies the project and gives a contact URL, the
maxlag parameter so we back off when their replication lags, and an
exponential retry that honours Retry-After instead of hammering.

Two quality gates matter downstream. MIN_WIDTH keeps genuinely small
scans out. BOOK_SCAN rejects halftones lifted from printed books: those
are dot screens, not photographs, and they fall apart the moment they
are enlarged to fill a vertical video frame. manifest records
video_ready so the picker can demand a higher bar for reels than for
cards without a second harvest.

Writes report.json next to manifest.json so every category's yield and
any error is visible without opening the Action log.
"""
import json, os, re, time, hashlib
import requests

API = "https://commons.wikimedia.org/w/api.php"
UA = ("fb-archive-harvester/1.2 "
      "(https://github.com/damonspencer0421-wq/fb-archive; archival research)")
MIN_WIDTH = 1400          # anything narrower is not worth storing
VIDEO_MIN_WIDTH = 2500    # vertical video crops hard; below this it goes soft
MAX_BYTES = 9000000
PER_CATEGORY = 40
SUBCAT_LIMIT = 10
MAX_EDGE = 2400
PACE = 1.1
ROOT = os.path.dirname(os.path.abspath(__file__))
OK_LICENSE = re.compile(r"public domain|^pd|cc0|no known copyright|cc.by", re.I)
SKIP_SUBCAT = re.compile(r"media needing|categories requiring|to be checked|"
                         r"unidentified|flags of|maps of|coats of arms", re.I)
# printed halftones, engravings and book plates: real sources, wrong texture
BOOK_SCAN = re.compile(r"boys' life of|boys life of|illustrated london|"
                       r"harper's weekly|frank leslie|engraving|lithograph|"
                       r"woodcut|book plate|bookplate|title page|frontispiece|"
                       r"\bplate \d|magazine cover|sheet music", re.I)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})


def api(params, tries=6):
    p = {"format": "json", "formatversion": "2", "maxlag": "5"}
    p.update(params)
    delay = 2.0
    last = None
    for attempt in range(tries):
        try:
            r = SESSION.get(API, params=p, timeout=60)
            if r.status_code in (429, 503):
                wait = float(r.headers.get("Retry-After", delay))
                print("   throttled, sleeping %.0fs" % wait)
                time.sleep(min(wait, 60))
                delay = min(delay * 2, 60)
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d and d["error"].get("code") == "maxlag":
                time.sleep(min(delay, 30))
                delay = min(delay * 2, 60)
                continue
            time.sleep(PACE)
            return d
        except requests.RequestException as e:
            last = e
            time.sleep(min(delay, 30))
            delay = min(delay * 2, 60)
    raise RuntimeError("gave up after %d tries: %s" % (tries, last))


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
    rpath = os.path.join(ROOT, "report.json")
    report = json.load(open(rpath)) if os.path.exists(rpath) else {}
    added = 0

    for page, cats in targets.items():
        outdir = os.path.join(ROOT, "images", page)
        os.makedirs(outdir, exist_ok=True)
        for cat in cats:
            rec = {"page": page, "exists": None, "candidates": 0,
                   "too_small": 0, "book_scan": 0, "wrong_license": 0,
                   "already_had": 0, "kept": 0, "video_ready": 0}
            report[cat] = rec
            rec["exists"] = cat_exists(cat)
            if rec["exists"] is False:
                print("MISSING CATEGORY", cat)
                continue
            try:
                files = gather(cat, PER_CATEGORY)
            except Exception as e:
                rec["error"] = str(e)[:200]
                print("FAILED", cat, rec["error"])
                continue
            rec["candidates"] = len(files)

            for f in files:
                title = f.get("title", "")
                ii = (f.get("imageinfo") or [{}])[0]
                if ii.get("width", 0) < MIN_WIDTH:
                    rec["too_small"] += 1
                    continue
                if BOOK_SCAN.search(title):
                    rec["book_scan"] += 1
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
                    r = SESSION.get(url, timeout=120)
                    r.raise_for_status()
                    if len(r.content) > MAX_BYTES:
                        continue
                    with open(dest, "wb") as fh:
                        fh.write(r.content)
                except Exception:
                    continue

                ready = ii.get("width", 0) >= VIDEO_MIN_WIDTH
                manifest[key] = {
                    "page": page,
                    "category": cat,
                    "path": "images/%s/%s" % (page, name),
                    "title": title,
                    "orig_width": ii.get("width"),
                    "orig_height": ii.get("height"),
                    "video_ready": ready,
                    "credit": field(meta, "Credit") or field(meta, "Artist"),
                    "description": field(meta, "ImageDescription"),
                    "date": field(meta, "DateTimeOriginal"),
                    "license": field(meta, "LicenseShortName"),
                    "source": ii.get("descriptionurl", ""),
                }
                added += 1
                rec["kept"] += 1
                if ready:
                    rec["video_ready"] += 1
                time.sleep(0.4)
            print(cat, rec)

            with open(mpath, "w") as fh:
                json.dump(manifest, fh, indent=1, sort_keys=True)
            with open(rpath, "w") as fh:
                json.dump(report, fh, indent=1, sort_keys=True)

    print("ADDED", added, "TOTAL", len(manifest))


if __name__ == "__main__":
    main()
