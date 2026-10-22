#!/usr/bin/env python3
"""
Steam tam katalog çekici — resumable (SQLite checkpoint).

Aşamalar:
  1) applist   : tüm oyun appid'leri  (STEAM_API_KEY varsa sadece 'game' tipi, yoksa tüm liste)
  2) details   : store appdetails (açıklama, tür, kategori, tarih, fiyat...)  ~1.5 s/istek
  3) reviews   : toplam review sayısı (population)
  4) spy       : SteamSpy tag'leri + owner tahmini

Kullanım:
  pip install requests
  export STEAM_API_KEY=xxxx        # https://steamcommunity.com/dev/apikey (opsiyonel ama önerilir)
  python steam_crawl.py            # kaldığı yerden devam eder, Ctrl+C güvenli
  python steam_crawl.py --stage details --limit 500   # tek aşama / deneme
  python steam_crawl.py --export games.jsonl          # analiz için dışa aktar
"""
import argparse, html, json, os, re, sqlite3, sys, time
from datetime import datetime
import requests

DB = os.environ.get("STEAM_DB", "steam.db")
UA = {"User-Agent": "Mozilla/5.0 (steam-map-research; contact: ahmetkadir.com)"}
S = requests.Session(); S.headers.update(UA)

DETAILS_INTERVAL = 1.55   # 200 istek / 5 dk sınırının hemen altında
REVIEWS_INTERVAL = 0.6
SPY_INTERVAL     = 1.05

# ---------------------------------------------------------------- db
def db():
    c = sqlite3.connect(DB, timeout=60)
    c.executescript("""
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS apps(
        appid INTEGER PRIMARY KEY, name TEXT,
        details_status TEXT, reviews_status TEXT, spy_status TEXT);
    CREATE TABLE IF NOT EXISTS details(
        appid INTEGER PRIMARY KEY, type TEXT, name TEXT, is_free INTEGER,
        short_description TEXT, about TEXT, genres TEXT, categories TEXT,
        developers TEXT, publishers TEXT, release_date TEXT, coming_soon INTEGER,
        price_cents INTEGER, currency TEXT, header_image TEXT, metacritic INTEGER,
        recommendations INTEGER, platforms TEXT, raw TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS reviews(
        appid INTEGER PRIMARY KEY, total INTEGER, positive INTEGER, negative INTEGER,
        score_desc TEXT, fetched_at TEXT);
    CREATE TABLE IF NOT EXISTS spy(
        appid INTEGER PRIMARY KEY, tags TEXT, owners TEXT, average_forever INTEGER,
        ccu INTEGER, fetched_at TEXT);
    CREATE INDEX IF NOT EXISTS idx_type ON details(type);
    """)
    return c

def now(): return datetime.utcnow().isoformat(timespec="seconds")

def strip_html(s):
    if not s: return ""
    s = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()

# ---------------------------------------------------------------- http
def get(url, params=None, interval=1.0, tries=6):
    """429/5xx'te üstel bekleme. None dönerse pes edildi demektir."""
    backoff = 60
    for i in range(tries):
        try:
            r = S.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"   ! net err {e.__class__.__name__}, {backoff}s bekle"); time.sleep(backoff); backoff *= 2; continue
        if r.status_code == 200:
            time.sleep(interval); return r
        if r.status_code in (429, 403, 500, 502, 503):
            print(f"   ! HTTP {r.status_code}, {backoff}s bekle"); time.sleep(backoff); backoff = min(backoff * 2, 900); continue
        if r.status_code == 404:
            time.sleep(interval); return r
        print(f"   ! HTTP {r.status_code} -> atla"); time.sleep(interval); return r
    return None

# ---------------------------------------------------------------- 1) applist
def stage_applist(c):
    key = os.environ.get("STEAM_API_KEY")
    apps = []
    if key:
        print("applist: IStoreService (sadece oyunlar)")
        last = 0
        while True:
            r = get("https://api.steampowered.com/IStoreService/GetAppList/v1/",
                    {"key": key, "include_games": "true", "include_dlc": "false",
                     "include_software": "false", "include_videos": "false",
                     "include_hardware": "false", "max_results": 50000, "last_appid": last}, interval=0.5)
            d = r.json().get("response", {})
            batch = d.get("apps", [])
            apps += [(a["appid"], a.get("name", "")) for a in batch]
            print(f"   +{len(batch)} (toplam {len(apps)})")
            if not d.get("have_more_results"): break
            last = d["last_appid"]
    else:
        print("applist: ISteamApps v2 (API key yok -> DLC/demo dahil, details aşamasında elenir)")
        r = get("https://api.steampowered.com/ISteamApps/GetAppList/v2/", interval=0.5)
        apps = [(a["appid"], a.get("name", "")) for a in r.json()["applist"]["apps"]]
    c.executemany("INSERT OR IGNORE INTO apps(appid,name) VALUES(?,?)", apps)
    c.commit()
    print(f"applist: {c.execute('SELECT COUNT(*) FROM apps').fetchone()[0]} appid kayıtlı")

# ---------------------------------------------------------------- 2) details
def stage_details(c, limit=None):
    rows = c.execute("SELECT appid FROM apps WHERE details_status IS NULL ORDER BY appid").fetchall()
    if limit: rows = rows[:limit]
    total = len(rows); print(f"details: {total} bekliyor (~{total*DETAILS_INTERVAL/3600:.1f} saat)")
    t0 = time.time()
    for i, (appid,) in enumerate(rows, 1):
        r = get("https://store.steampowered.com/api/appdetails",
                {"appids": appid, "cc": "us", "l": "english"}, interval=DETAILS_INTERVAL)
        if r is None:
            c.execute("UPDATE apps SET details_status='err' WHERE appid=?", (appid,)); c.commit(); continue
        try:
            d = r.json().get(str(appid), {})
        except ValueError:
            d = {}
        if not d.get("success"):
            c.execute("UPDATE apps SET details_status='missing' WHERE appid=?", (appid,))
        else:
            x = d["data"]
            rd = x.get("release_date", {}) or {}
            po = x.get("price_overview") or {}
            c.execute("""INSERT OR REPLACE INTO details VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                appid, x.get("type"), x.get("name"), int(bool(x.get("is_free"))),
                strip_html(x.get("short_description")), strip_html(x.get("about_the_game")),
                json.dumps([g["description"] for g in x.get("genres", [])], ensure_ascii=False),
                json.dumps([g["description"] for g in x.get("categories", [])], ensure_ascii=False),
                json.dumps(x.get("developers", []), ensure_ascii=False),
                json.dumps(x.get("publishers", []), ensure_ascii=False),
                rd.get("date"), int(bool(rd.get("coming_soon"))),
                po.get("final"), po.get("currency"), x.get("header_image"),
                (x.get("metacritic") or {}).get("score"), (x.get("recommendations") or {}).get("total"),
                json.dumps(x.get("platforms", {})), json.dumps(x, ensure_ascii=False), now()))
            c.execute("UPDATE apps SET details_status=? WHERE appid=?", (x.get("type"), appid))
        if i % 25 == 0:
            c.commit()
            rate = i / (time.time() - t0)
            print(f"   {i}/{total}  {rate*3600:.0f}/saat  kalan ~{(total-i)/rate/3600:.1f} saat")
    c.commit()

# ---------------------------------------------------------------- 3) reviews
def stage_reviews(c, limit=None):
    rows = c.execute("""SELECT a.appid FROM apps a JOIN details d USING(appid)
                        WHERE d.type='game' AND a.reviews_status IS NULL ORDER BY a.appid""").fetchall()
    if limit: rows = rows[:limit]
    total = len(rows); print(f"reviews: {total} bekliyor (~{total*REVIEWS_INTERVAL/3600:.1f} saat)")
    for i, (appid,) in enumerate(rows, 1):
        r = get(f"https://store.steampowered.com/appreviews/{appid}",
                {"json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0}, interval=REVIEWS_INTERVAL)
        if r is None:
            c.execute("UPDATE apps SET reviews_status='err' WHERE appid=?", (appid,)); continue
        try:
            q = r.json().get("query_summary", {})
            c.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,?)",
                      (appid, q.get("total_reviews", 0), q.get("total_positive", 0),
                       q.get("total_negative", 0), q.get("review_score_desc"), now()))
            c.execute("UPDATE apps SET reviews_status='ok' WHERE appid=?", (appid,))
        except ValueError:
            c.execute("UPDATE apps SET reviews_status='err' WHERE appid=?", (appid,))
        if i % 50 == 0: c.commit(); print(f"   {i}/{total}")
    c.commit()

# ---------------------------------------------------------------- 4) steamspy
def stage_spy(c, limit=None):
    rows = c.execute("""SELECT a.appid FROM apps a JOIN details d USING(appid)
                        WHERE d.type='game' AND a.spy_status IS NULL ORDER BY a.appid""").fetchall()
    if limit: rows = rows[:limit]
    total = len(rows); print(f"spy: {total} bekliyor (~{total*SPY_INTERVAL/3600:.1f} saat)")
    for i, (appid,) in enumerate(rows, 1):
        r = get("https://steamspy.com/api.php", {"request": "appdetails", "appid": appid}, interval=SPY_INTERVAL)
        if r is None:
            c.execute("UPDATE apps SET spy_status='err' WHERE appid=?", (appid,)); continue
        try:
            d = r.json()
            tags = d.get("tags") or {}
            if isinstance(tags, list): tags = {}
            c.execute("INSERT OR REPLACE INTO spy VALUES(?,?,?,?,?,?)",
                      (appid, json.dumps(tags, ensure_ascii=False), d.get("owners"),
                       d.get("average_forever"), d.get("ccu"), now()))
            c.execute("UPDATE apps SET spy_status='ok' WHERE appid=?", (appid,))
        except ValueError:
            c.execute("UPDATE apps SET spy_status='err' WHERE appid=?", (appid,))
        if i % 50 == 0: c.commit(); print(f"   {i}/{total}")
    c.commit()

# ---------------------------------------------------------------- export
def export(c, path):
    q = """SELECT d.appid,d.name,d.short_description,d.about,d.genres,d.categories,d.developers,
                  d.publishers,d.release_date,d.is_free,d.price_cents,d.header_image,d.metacritic,
                  r.total,r.positive,r.negative,s.tags,s.owners
           FROM details d LEFT JOIN reviews r USING(appid) LEFT JOIN spy s USING(appid)
           WHERE d.type='game' AND d.coming_soon=0 AND length(d.short_description)>20"""
    n = 0
    with open(path, "w", encoding="utf-8") as f:
        for row in c.execute(q):
            (appid, name, sd, about, genres, cats, devs, pubs, rel, free, price, img, mc, tot, pos, neg, tags, owners) = row
            f.write(json.dumps({
                "appid": appid, "name": name, "short_description": sd, "about": about,
                "genres": json.loads(genres or "[]"), "categories": json.loads(cats or "[]"),
                "developers": json.loads(devs or "[]"), "publishers": json.loads(pubs or "[]"),
                "release_date": rel, "is_free": bool(free), "price_usd": (price or 0) / 100,
                "header_image": img, "metacritic": mc,
                "reviews_total": tot or 0, "reviews_positive": pos or 0, "reviews_negative": neg or 0,
                "tags": json.loads(tags or "{}"), "owners": owners}, ensure_ascii=False) + "\n")
            n += 1
    print(f"export: {n} oyun -> {path}")

def status(c):
    print("apps      :", c.execute("SELECT COUNT(*) FROM apps").fetchone()[0])
    for col in ("details_status", "reviews_status", "spy_status"):
        print(f"{col:14}:", dict(c.execute(f"SELECT COALESCE({col},'TODO'),COUNT(*) FROM apps GROUP BY 1").fetchall()))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["all", "applist", "details", "reviews", "spy"], default="all")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--export"); ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    c = db()
    try:
        if a.status: status(c); sys.exit()
        if a.export: export(c, a.export); sys.exit()
        if a.stage in ("all", "applist") and c.execute("SELECT COUNT(*) FROM apps").fetchone()[0] == 0:
            stage_applist(c)
        if a.stage in ("all", "details"): stage_details(c, a.limit)
        if a.stage in ("all", "reviews"): stage_reviews(c, a.limit)
        if a.stage in ("all", "spy"):     stage_spy(c, a.limit)
        status(c)
    except KeyboardInterrupt:
        c.commit(); print("\nDurduruldu, ilerleme kaydedildi."); status(c)
