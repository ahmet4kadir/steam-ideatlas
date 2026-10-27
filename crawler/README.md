# steam-ideatlas on Coolify

Tek compose, dört servis, ortak `steamdata` volume'u:

| servis   | ne yapar                                         | ne zaman        |
|----------|--------------------------------------------------|-----------------|
| details  | Steam store appdetails → steam.db                | bitti, restart yok |
| spy      | SteamSpy tag + review sayıları → steam.db        | bitti, restart yok |
| pipeline | embedding → UMAP → kümeler → boşluklar → /data/web | tek seferlik, 2-4 saat |
| web      | nginx: viewer + /data/web                        | sürekli, domain buna |

Env (Coolify → Environment Variables): `STEAM_API_KEY` (crawler), isteğe bağlı `EMBED_BACKEND=openai` + `OPENAI_API_KEY`.

Durum / export:
    docker exec -it <spy-container> python steam_crawl.py --status
    docker exec -it <spy-container> python steam_crawl.py --export /data/games.jsonl

Pipeline'ı bir aşamadan yeniden koşturmak:
    docker compose run --rm pipeline python run.py --force umap
