#!/usr/bin/env python3
"""
Steam Ideatlas pipeline: steam.db  ->  embeddings -> 3D map -> clusters -> holes -> web/data

Her aşama /data/pipeline/ altına cache'ler; tekrar çalıştırınca biteni atlar.
    python run.py                # hepsi
    python run.py --force umap   # bir aşamayı (ve sonrakileri) yeniden yap
Env:
    STEAM_DB=/data/steam.db   OUT=/data/pipeline   WEB=/data/web
    EMBED_BACKEND=local|openai|random   EMBED_MODEL=BAAI/bge-small-en-v1.5   OPENAI_API_KEY=...
"""
import argparse, json, math, os, sqlite3, sys, time
from collections import Counter
import numpy as np

DB  = os.environ.get("STEAM_DB", "/data/steam.db")
OUT = os.environ.get("OUT", "/data/pipeline")
WEB = os.environ.get("WEB", "/data/web")
BACKEND = os.environ.get("EMBED_BACKEND", "local")
MODEL   = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
STAGES = ["load", "embed", "nn", "umap", "clusters", "holes", "web"]

def log(*a): print(time.strftime("%H:%M:%S"), *a, flush=True)
def p(name): return os.path.join(OUT, name)

# ------------------------------------------------------------------ 1) load
def stage_load():
    c = sqlite3.connect(DB)
    q = """SELECT d.appid, d.name, d.short_description, d.about, d.genres, d.release_date,
                  d.is_free, d.price_cents, d.header_image, d.developers,
                  s.tags, s.positive, s.negative, s.owners
           FROM details d LEFT JOIN spy s USING(appid)
           WHERE d.type='game' AND d.coming_soon=0
             AND d.short_description IS NOT NULL AND length(d.short_description) > 20
           ORDER BY d.appid"""
    games, texts = [], []
    for row in c.execute(q):
        appid, name, sd, about, genres, rel, free, price, img, devs, tags, pos, neg, owners = row
        tags = json.loads(tags) if tags else {}
        if isinstance(tags, list): tags = {}
        top_tags = [t for t, _ in sorted(tags.items(), key=lambda kv: -kv[1])[:10]]
        reviews = (pos or 0) + (neg or 0)
        games.append({"appid": appid, "name": name, "short": sd, "genres": json.loads(genres or "[]"),
                      "tags": top_tags, "release": rel, "free": bool(free), "price": (price or 0) / 100,
                      "img": img, "devs": json.loads(devs or "[]")[:2],
                      "reviews": reviews, "positive": pos or 0, "owners": owners})
        about = (about or "")[:800].replace("\n", " ")
        texts.append(f"{name}. {sd} Tags: {', '.join(top_tags)}. {about}")
    json.dump(games, open(p("games.json"), "w"), ensure_ascii=False)
    json.dump(texts, open(p("texts.json"), "w"), ensure_ascii=False)
    log(f"load: {len(games)} oyun")

# ------------------------------------------------------------------ 2) embed
def embed_local(texts):
    from sentence_transformers import SentenceTransformer
    m = SentenceTransformer(MODEL, device="cpu"); m.max_seq_length = 256
    CH = 4096; parts = []
    for i in range(0, len(texts), CH):
        f = p(f"emb_part_{i//CH:04d}.npy")
        if not os.path.exists(f):
            v = m.encode(texts[i:i+CH], batch_size=64, normalize_embeddings=True, show_progress_bar=False)
            np.save(f, v.astype(np.float16))
            log(f"embed: {min(i+CH, len(texts))}/{len(texts)}")
        parts.append(np.load(f))
    return np.vstack(parts).astype(np.float32)

def embed_openai(texts):
    from openai import OpenAI
    cl = OpenAI(); model = os.environ.get("EMBED_MODEL", "text-embedding-3-small")
    CH = 2048; parts = []
    for i in range(0, len(texts), CH):
        f = p(f"emb_part_{i//CH:04d}.npy")
        if not os.path.exists(f):
            vs = []
            for j in range(i, min(i+CH, len(texts)), 256):
                batch = [t[:6000] for t in texts[j:j+256]]
                for attempt in range(6):
                    try:
                        r = cl.embeddings.create(model=model, input=batch); break
                    except Exception as e:
                        log(f"   ! openai {e.__class__.__name__}, bekle"); time.sleep(10 * (attempt + 1))
                else: raise RuntimeError("openai embed fail")
                vs += [d.embedding for d in r.data]
            np.save(f, np.asarray(vs, dtype=np.float16)); log(f"embed: {min(i+CH, len(texts))}/{len(texts)}")
        parts.append(np.load(f))
    v = np.vstack(parts).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)

def stage_embed():
    texts = json.load(open(p("texts.json")))
    if BACKEND == "random":   # test
        rng = np.random.default_rng(0); k = 12   # test: ilk tag'e göre küme
        centers = rng.normal(size=(k, 64))
        lab = np.array([hash(t.split("Tags: ")[1].split(",")[0]) % k if "Tags: " in t else 0 for t in texts])
        v = centers[lab] + rng.normal(scale=0.6, size=(len(texts), 64))
        v /= np.linalg.norm(v, axis=1, keepdims=True)
    elif BACKEND == "openai": v = embed_openai(texts)
    else: v = embed_local(texts)
    np.save(p("emb.npy"), v.astype(np.float16)); log(f"embed: {v.shape}")

# ------------------------------------------------------------------ 3) nearest neighbour
def stage_nn():
    v = np.load(p("emb.npy")).astype(np.float32); n = len(v)
    sim = np.zeros(n, np.float32); idx = np.zeros(n, np.int32); CH = 512
    for i in range(0, n, CH):
        s = v[i:i+CH] @ v.T
        for r in range(s.shape[0]): s[r, i + r] = -2       # kendisi hariç
        idx[i:i+CH] = s.argmax(1); sim[i:i+CH] = s.max(1)
        if (i // CH) % 40 == 0: log(f"nn: {i}/{n}")
    np.save(p("nn_sim.npy"), sim); np.save(p("nn_idx.npy"), idx)
    log(f"nn: medyan sim {np.median(sim):.3f}")

# ------------------------------------------------------------------ 4) umap
def stage_umap():
    import umap
    v = np.load(p("emb.npy")).astype(np.float32)
    red = umap.UMAP(n_components=3, n_neighbors=15, min_dist=0.05, metric="cosine",
                    random_state=42, low_memory=True, verbose=True)
    pos = red.fit_transform(v).astype(np.float32)
    pos -= pos.mean(0); pos /= np.abs(pos).max()      # [-1, 1]
    np.save(p("pos.npy"), pos); log("umap: ok")

GENERIC = {"Indie", "Singleplayer", "Casual", "Early Access", "Free to Play", "Action", "Adventure", "2D", "3D",
           "Great Soundtrack", "Atmospheric", "Colorful", "Cute", "Funny", "Family Friendly", "Multiplayer", "Simulation", "Strategy"}

# ------------------------------------------------------------------ 5) clusters
def stage_clusters():
    from sklearn.cluster import HDBSCAN
    from scipy.spatial import cKDTree
    pos = np.load(p("pos.npy")); games = json.load(open(p("games.json"))); n = len(pos)
    mcs = max(30, n // 1200)
    lab = HDBSCAN(min_cluster_size=mcs, min_samples=10, cluster_selection_method="eom").fit_predict(pos)
    # gürültü noktalarını en yakın kümeli komşuya ata (renk sürekliliği)
    noise = lab < 0
    if noise.any() and (~noise).any():
        t = cKDTree(pos[~noise]); _, j = t.query(pos[noise], k=1); lab[noise] = lab[~noise][j]
    uniq = np.unique(lab); remap = {int(u): i for i, u in enumerate(uniq)}; lab = np.array([remap[int(l)] for l in lab], np.int32)
    k = len(uniq)
    # etiket: küme içinde lift'i en yüksek tag'ler
    glob = Counter(); per = [Counter() for _ in range(k)]
    for g, l in zip(games, lab):
        for t in g["tags"][:6]: glob[t] += 1; per[l][t] += 1
    clusters = []
    for ci in range(k):
        m = lab == ci; cnt = int(m.sum()); cen = pos[m].mean(0)
        sc = {t: (c_ / cnt) * max(0.0, (c_ / cnt) / (glob[t] / n) - 1.0)
              for t, c_ in per[ci].items() if c_ >= max(3, cnt * 0.1) and t not in GENERIC}
        top = [t for t, _ in sorted(sc.items(), key=lambda kv: -kv[1])[:3]]
        rv = sum(games[i]["reviews"] for i in np.where(m)[0])
        clusters.append({"id": ci, "n": cnt, "center": [round(float(x), 4) for x in cen],
                         "label": " · ".join(top) or "misc", "tags": top, "reviews": int(rv),
                         "hue": round((ci * 137.508) % 360, 1)})
    np.save(p("labels.npy"), lab); json.dump(clusters, open(p("clusters.json"), "w"), ensure_ascii=False)
    log(f"clusters: {k} küme (min_cluster_size={mcs}), gürültü {int(noise.sum())}")

# ------------------------------------------------------------------ 6) holes
def stage_holes():
    from scipy.spatial import cKDTree
    pos = np.load(p("pos.npy")); games = json.load(open(p("games.json"))); lab = np.load(p("labels.npy"))
    clusters = json.load(open(p("clusters.json")))
    n = len(pos); K = 30; tree = cKDTree(pos)
    gd, _ = tree.query(pos, k=K + 1); g1, gK = gd[:, 1], gd[:, K]
    inside = np.percentile(gK, 85)       # bulutun "içi" sayılacak yoğunluk eşiği
    rng = np.random.default_rng(0)
    lo, hi = pos.min(0), pos.max(0)
    cand = rng.uniform(lo, hi, size=(400_000, 3))
    cd, ci = tree.query(cand, k=K)
    ok = cd[:, K - 1] <= inside
    cand, cd, ci = cand[ok], cd[ok], ci[ok]
    order = np.argsort(-cd[:, 0])
    sep = float(np.median(gK)) * 2.0
    holes, taken = [], []
    for o in order:
        q = cand[o]
        if any(np.linalg.norm(q - t) < sep for t in taken): continue
        taken.append(q)
        nb = ci[o][:8]
        tags = Counter(t for i in nb for t in games[i]["tags"][:5]).most_common(5)
        holes.append({"id": len(holes), "pos": [round(float(x), 4) for x in q],
                      "radius": round(float(cd[o, 0]), 4),
                      "gap": round(float(cd[o, 0] / np.median(g1)), 2),  # tipik komşu mesafesine oran
                      "neighbors": [{"appid": games[i]["appid"], "name": games[i]["name"],
                                     "cluster": int(lab[i])} for i in nb],
                      "tags": [t for t, _ in tags],
                      "clusters": sorted({int(lab[i]) for i in nb})})
        if len(holes) >= 40: break
    json.dump(holes, open(p("holes.json"), "w"), ensure_ascii=False)
    log(f"holes: {len(holes)} boşluk, en büyük gap x{holes[0]['gap'] if holes else 0}")

# ------------------------------------------------------------------ 7) web export
def stage_web():
    os.makedirs(os.path.join(WEB, "games"), exist_ok=True)
    pos = np.load(p("pos.npy")); lab = np.load(p("labels.npy")); sim = np.load(p("nn_sim.npy")); nidx = np.load(p("nn_idx.npy"))
    games = json.load(open(p("games.json"))); clusters = json.load(open(p("clusters.json"))); holes = json.load(open(p("holes.json")))
    n = len(games)
    # ikili dosyalar
    (pos * 32767).round().astype("<i2").tofile(os.path.join(WEB, "pos.bin"))
    lab.astype("<u2").tofile(os.path.join(WEB, "cluster.bin"))
    rv = np.array([g["reviews"] for g in games], np.float64)
    size = np.clip(np.log1p(rv) / np.log1p(rv.max() + 1) * 255, 0, 255).astype(np.uint8)
    size.tofile(os.path.join(WEB, "size.bin"))
    # yalnızlık yüzdeliği: bu oyundan daha yakın ikizi olan oyunların oranı
    rank = np.argsort(np.argsort(-sim)); twin_pct = (rank / n * 100)
    np.clip(twin_pct * 2.55, 0, 255).astype(np.uint8).tofile(os.path.join(WEB, "lonely.bin"))
    json.dump([[g["appid"], g["name"]] for g in games], open(os.path.join(WEB, "names.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump(clusters, open(os.path.join(WEB, "clusters.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    json.dump(holes, open(os.path.join(WEB, "holes.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    # detay parçaları: appid % 1024
    chunks = {}
    for i, g in enumerate(games):
        t = games[int(nidx[i])]
        chunks.setdefault(g["appid"] % 1024, {})[str(g["appid"])] = {
            "i": i, "name": g["name"], "short": g["short"], "tags": g["tags"][:8], "genres": g["genres"][:4],
            "release": g["release"], "free": g["free"], "price": g["price"], "img": g["img"], "devs": g["devs"],
            "reviews": g["reviews"], "positive": g["positive"], "cluster": int(lab[i]),
            "twin": {"appid": t["appid"], "name": t["name"], "sim": round(float(sim[i]), 3)},
            "twin_pct": round(float(twin_pct[i]), 1)}
    for k, v in chunks.items():
        json.dump(v, open(os.path.join(WEB, "games", f"{k}.json"), "w"), ensure_ascii=False, separators=(",", ":"))
    loners = [int(i) for i in np.argsort(sim)[:50]]
    stats = {"games": n, "clusters": len(clusters), "holes": len(holes),
             "reviews_total": int(rv.sum()), "median_twin_sim": round(float(np.median(sim)), 3),
             "built": time.strftime("%Y-%m-%d"),
             "loners": [{"appid": games[i]["appid"], "name": games[i]["name"], "sim": round(float(sim[i]), 3)} for i in loners],
             "top_clusters": sorted([{"id": c["id"], "label": c["label"], "n": c["n"], "reviews": c["reviews"]} for c in clusters],
                                    key=lambda c: -c["reviews"])[:12]}
    json.dump(stats, open(os.path.join(WEB, "stats.json"), "w"), ensure_ascii=False)
    log(f"web: {n} oyun -> {WEB}")

DONE_FILE = {"load": "texts.json", "embed": "emb.npy", "nn": "nn_sim.npy", "umap": "pos.npy",
             "clusters": "clusters.json", "holes": "holes.json", "web": None}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--force", choices=STAGES); a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True); os.makedirs(WEB, exist_ok=True)
    force = False
    for st in STAGES:
        if a.force == st: force = True
        f = DONE_FILE[st]
        if not force and f and os.path.exists(p(f)): log(f"{st}: cache"); continue
        log(f"== {st}"); t0 = time.time(); globals()[f"stage_{st}"](); log(f"== {st} bitti ({time.time()-t0:.0f}s)")
    open(os.path.join(WEB, "READY"), "w").write(time.strftime("%Y-%m-%dT%H:%M:%S"))
