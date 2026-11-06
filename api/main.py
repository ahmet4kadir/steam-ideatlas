"""
Ideatlas idea API — "what lives in the empty space?"

POST /api/idea   {mode: "blend", appids: [..2-3]}  |  {mode: "hole", hole_id: n}
                 -> text/event-stream: target, draft (xN), final, error
GET  /api/ideas  ?limit=50   -> previously generated ideas (free, no LLM)
GET  /api/idea/{id}
GET  /api/health

Loop (like the video): Claude writes a concept -> we embed it with the same model the
map was built with -> measure closest existing game + how far it is from the target ->
if too close to something that exists, feed that back and ask for another draft.

Env: ANTHROPIC_API_KEY, ANTHROPIC_MODEL (default claude-sonnet-5-5), PIPE=/data/pipeline,
     IDEAS=/data/ideas, EMBED_MODEL, HF_HOME, MAX_DRAFTS=3, PER_IP_PER_HOUR=6, DAILY_CAP=300,
     EMBED_BACKEND=local|random (random = test), ANTHROPIC_MOCK=1 (test, no API calls)
"""
import asyncio, hashlib, json, os, re, time, uuid
from collections import defaultdict, deque
import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

PIPE = os.environ.get("PIPE", "/data/pipeline")
IDEAS_DIR = os.environ.get("IDEAS", "/data/ideas")
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
EMBED_MODEL = os.environ.get("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
MAX_DRAFTS = int(os.environ.get("MAX_DRAFTS", "3"))
PER_IP = int(os.environ.get("PER_IP_PER_HOUR", "6"))
DAILY_CAP = int(os.environ.get("DAILY_CAP", "300"))
MOCK = os.environ.get("ANTHROPIC_MOCK") == "1"

app = FastAPI(title="ideatlas-api")
S = {}  # state

# ------------------------------------------------------------------ load
def load():
    t0 = time.time()
    emb = np.load(os.path.join(PIPE, "emb.npy")).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True) + 1e-9
    games = json.load(open(os.path.join(PIPE, "games.json")))
    pos = np.load(os.path.join(PIPE, "pos.npy"))
    lab = np.load(os.path.join(PIPE, "labels.npy"))
    nn_sim = np.load(os.path.join(PIPE, "nn_sim.npy"))
    holes = json.load(open(os.path.join(PIPE, "holes.json")))
    clusters = json.load(open(os.path.join(PIPE, "clusters.json")))
    S.update(emb=emb, games=games, pos=pos, lab=lab, holes=holes, clusters=clusters,
             nn_sorted=np.sort(nn_sim), appid_to_i={g["appid"]: i for i, g in enumerate(games)})
    if os.environ.get("EMBED_BACKEND", "local") == "random":
        S["embed"] = _embed_random
    else:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer(EMBED_MODEL, device="cpu"); m.max_seq_length = 256
        S["embed"] = lambda text: m.encode([text], normalize_embeddings=True)[0].astype(np.float32)
    os.makedirs(IDEAS_DIR, exist_ok=True)
    S["ideas"] = _load_ideas()
    print(f"loaded {len(games)} games, {len(S['ideas'])} ideas in {time.time()-t0:.0f}s", flush=True)

def _embed_random(text):
    rng = np.random.default_rng(int(hashlib.md5(text.encode()).hexdigest()[:8], 16))
    v = rng.normal(size=S["emb"].shape[1]).astype(np.float32); return v / np.linalg.norm(v)

def _ideas_path(): return os.path.join(IDEAS_DIR, "ideas.jsonl")
def _load_ideas():
    out = []
    if os.path.exists(_ideas_path()):
        for line in open(_ideas_path(), encoding="utf-8"):
            try: out.append(json.loads(line))
            except ValueError: pass
    return out

@app.on_event("startup")
def _startup(): load()

# ------------------------------------------------------------------ helpers
def twin_pct(sim):
    """% of Steam games whose closest twin is MORE similar than this (higher = more novel)."""
    return float(np.searchsorted(S["nn_sorted"], sim) / len(S["nn_sorted"]) * 100)

def closest(v, k=5, exclude=()):
    sims = S["emb"] @ v
    for i in exclude: sims[i] = -2
    idx = np.argpartition(-sims, k)[:k]; idx = idx[np.argsort(-sims[idx])]
    return [(int(i), float(sims[i])) for i in idx]

def place(v):
    """Approximate map position: similarity-weighted mean of the 12 nearest games."""
    nb = closest(v, 12); w = np.array([max(s, 0.01) for _, s in nb]) ** 4
    p = (S["pos"][[i for i, _ in nb]] * w[:, None]).sum(0) / w.sum()
    return [round(float(x), 4) for x in p]

def brief(i, n=220):
    g = S["games"][i]; s = g["short"]
    return f"- {g['name']} ({', '.join(g['tags'][:4])}): {s[:n]}{'…' if len(s) > n else ''}"

def fmt_pct(x): return f"{x:.1f}"

# rate limits
ip_hits = defaultdict(deque); day = {"date": None, "n": 0}
def check_limits(ip):
    now = time.time(); q = ip_hits[ip]
    while q and now - q[0] > 3600: q.popleft()
    if len(q) >= PER_IP: raise HTTPException(429, f"Easy there — {PER_IP} ideas per hour per visitor. Browse the gallery meanwhile.")
    today = time.strftime("%Y-%m-%d")
    if day["date"] != today: day.update(date=today, n=0)
    if day["n"] >= DAILY_CAP: raise HTTPException(429, "Today's idea budget is spent. Come back tomorrow, or browse what others found.")
    q.append(now); day["n"] += 1

# ------------------------------------------------------------------ LLM
SYSTEM = """You are a game designer helping map the space of all Steam games. You write concise, concrete, pitchable game concepts.
Always answer with strict JSON: {"title": str, "pitch": str, "hook": str, "mechanics": [str, str, str], "tags": [str, ...]}.
- pitch: 2-3 sentences, present tense, no marketing fluff, describes what the player actually does.
- hook: one line, the single thing that makes it unlike anything else.
- mechanics: three specific systems or rules.
- tags: 4-6 Steam-style tags.
Do not reference or name existing games. Do not use the words "unique", "innovative", "revolutionary"."""

def first_prompt(mode, sources, extra):
    if mode == "blend":
        return (f"Design a game that sits exactly between these games — it should feel like a true descendant of all of them at once, "
                f"not a sequel to any one of them:\n" + "\n".join(sources) + "\n\nWrite the concept as JSON.")
    return (f"On a map of every Steam game there is an empty region. The games surrounding it are:\n" + "\n".join(sources) +
            f"\n\nThe region's dominant tags are: {extra}. Nothing exists in the middle. Design the game that would live there — "
            f"sharing the neighbourhood's DNA but unlike each neighbour. Write the concept as JSON.")

def feedback_prompt(draft, twin, sim, pct, mode, drift):
    msg = (f"Measured your draft against all {len(S['games']):,} Steam games. Closest existing game: \"{twin}\" "
           f"(cosine similarity {sim:.2f}; only {pct:.1f}% of Steam games are this far from their nearest twin). Too close — a player would say \"that's just {twin}\".\n")
    if drift: msg += f"Also: {drift}\n"
    msg += "Keep what made it fit the target, change what makes it resemble that game (the core loop, setting, or role of the player). Write a new concept as JSON."
    return msg

async def llm(messages):
    if MOCK:
        await asyncio.sleep(float(os.environ.get("ANTHROPIC_MOCK_DELAY", "0")))
        n = sum(1 for m in messages if m["role"] == "user")
        return json.dumps({"title": f"Mock Concept {n}", "pitch": f"Draft {n}: you tend a lighthouse that is also a courtroom; ships arrive with disputes and you rule by adjusting the light. Seed {uuid.uuid4().hex[:6]}.",
                           "hook": "Verdicts are navigation.", "mechanics": ["Light patterns as rulings", "Tide-based deadlines", "Reputation with ports"], "tags": ["Simulation", "Narrative", "Cozy", "Puzzle"]})
    from anthropic import AsyncAnthropic
    cl = AsyncAnthropic()
    r = await cl.messages.create(model=MODEL, max_tokens=700, system=SYSTEM, messages=messages)
    return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")

def parse_json(s):
    m = re.search(r"\{.*\}", s, re.S)
    d = json.loads(m.group(0) if m else s)
    d["mechanics"] = [str(x) for x in d.get("mechanics", [])][:3]; d["tags"] = [str(x) for x in d.get("tags", [])][:6]
    return {k: d.get(k, "") if k not in ("mechanics", "tags") else d[k] for k in ("title", "pitch", "hook", "mechanics", "tags")}

# ------------------------------------------------------------------ endpoint
class IdeaReq(BaseModel):
    mode: str
    appids: list[int] | None = None
    hole_id: int | None = None

def sse(event, data): return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

@app.post("/api/idea")
async def idea(req: IdeaReq, request: Request):
    ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    if req.mode == "blend":
        ids = [S["appid_to_i"][a] for a in (req.appids or []) if a in S["appid_to_i"]]
        if not 2 <= len(ids) <= 3: raise HTTPException(400, "pick 2 or 3 games")
        src = ids; target = S["emb"][ids].mean(0); target /= np.linalg.norm(target)
        extra = ""
    elif req.mode == "hole":
        if req.hole_id is None or not 0 <= req.hole_id < len(S["holes"]): raise HTTPException(400, "bad hole")
        h = S["holes"][req.hole_id]
        src = [S["appid_to_i"][n["appid"]] for n in h["neighbors"] if n["appid"] in S["appid_to_i"]][:8]
        target = S["emb"][src].mean(0); target /= np.linalg.norm(target); extra = ", ".join(h["tags"])
    else: raise HTTPException(400, "mode must be blend or hole")
    check_limits(ip)

    async def gen():
        try:
            yield sse("target", {"mode": req.mode, "sources": [{"appid": S["games"][i]["appid"], "name": S["games"][i]["name"], "pos": [float(x) for x in S["pos"][i]]} for i in src],
                                 "pos": place(target), "max_drafts": MAX_DRAFTS})
            messages = [{"role": "user", "content": first_prompt(req.mode, [brief(i) for i in src], extra)}]
            drafts = []; best = None
            for n in range(1, MAX_DRAFTS + 1):
                raw = await llm(messages)
                try: c = parse_json(raw)
                except Exception: c = {"title": "Untitled", "pitch": raw[:600], "hook": "", "mechanics": [], "tags": []}
                text = f"{c['title']}. {c['pitch']} {c['hook']} Tags: {', '.join(c['tags'])}. " + " ".join(c["mechanics"])
                v = await asyncio.get_event_loop().run_in_executor(None, S["embed"], text)
                near = closest(v, 5); ti, tsim = near[0]; pct = twin_pct(tsim)
                to_target = float(v @ target)
                src_sims = [float(v @ S["emb"][i]) for i in src]
                drift = ""
                if req.mode == "blend":
                    lo = int(np.argmin(src_sims)); hi = int(np.argmax(src_sims))
                    if src_sims[hi] - src_sims[lo] > 0.08:
                        drift = f"it leans too much toward \"{S['games'][src[hi]]['name']}\" and too little toward \"{S['games'][src[lo]]['name']}\" — rebalance."
                ok = pct >= 90.0 and not drift
                d = {"n": n, **c, "twin": {"appid": S["games"][ti]["appid"], "name": S["games"][ti]["name"], "sim": round(tsim, 3), "pos": [float(x) for x in S["pos"][ti]]},
                     "near": [{"appid": S["games"][i]["appid"], "name": S["games"][i]["name"], "sim": round(s, 3)} for i, s in near[1:4]],
                     "twin_pct": round(pct, 1), "to_target": round(to_target, 3),
                     "sources": [{"name": S["games"][i]["name"], "sim": round(s, 3)} for i, s in zip(src, src_sims)],
                     "pos": place(v), "verdict": "accepted" if ok else ("too close" if pct < 90 else "unbalanced")}
                drafts.append(d)
                if best is None or (d["twin_pct"] - (0 if not drift else 5)) > (best["twin_pct"] - (0 if best["verdict"] != "unbalanced" else 5)): best = d
                yield sse("draft", d)
                if ok or n == MAX_DRAFTS: break
                messages += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": feedback_prompt(c, S["games"][ti]["name"], tsim, pct, req.mode, drift)}]
            idea_id = uuid.uuid4().hex[:10]
            rec = {"id": idea_id, "ts": int(time.time()), "mode": req.mode, "model": MODEL,
                   "sources": [{"appid": S["games"][i]["appid"], "name": S["games"][i]["name"]} for i in src],
                   "hole_id": req.hole_id, "final": best, "drafts": [{k: d[k] for k in ("n", "title", "twin", "twin_pct", "verdict")} for d in drafts]}
            with open(_ideas_path(), "a", encoding="utf-8") as f: f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            S["ideas"].append(rec)
            yield sse("final", rec)
        except Exception as e:
            yield sse("error", {"message": f"{e.__class__.__name__}: {str(e)[:200]}"})
    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.get("/api/ideas")
def ideas(limit: int = 50):
    out = S["ideas"][-limit:][::-1]
    return [{"id": r["id"], "ts": r["ts"], "mode": r["mode"], "title": r["final"]["title"], "hook": r["final"]["hook"],
             "pos": r["final"]["pos"], "twin_pct": r["final"]["twin_pct"], "twin": r["final"]["twin"]["name"],
             "sources": [s["name"] for s in r["sources"]]} for r in out]

@app.get("/api/idea/{idea_id}")
def idea_get(idea_id: str):
    for r in S["ideas"]:
        if r["id"] == idea_id: return r
    raise HTTPException(404, "not found")

@app.get("/api/health")
def health(): return {"ok": True, "games": len(S.get("games", [])), "ideas": len(S.get("ideas", [])), "model": MODEL, "mock": MOCK}
