# steam-crawler (Coolify)

1. Coolify → New Resource → **Docker Compose** → bu repo/klasör.
2. Environment Variables: `STEAM_API_KEY=...`  (https://steamcommunity.com/dev/apikey)
3. Deploy. Üç container paralel çalışır, hepsi `steamdata` volume'undaki tek `steam.db`'ye yazar.

İlerleme:
    docker exec -it <details-container> python steam_crawl.py --status

Deneme (ilk deploy'dan önce, datacenter IP'si engelli mi diye):
    docker compose run --rm details python steam_crawl.py --stage applist
    docker compose run --rm details python steam_crawl.py --stage details --limit 100

Export (bitince):
    docker exec -it <details-container> python steam_crawl.py --export /data/games.jsonl
    docker cp <details-container>:/data/games.jsonl .

Notlar
- Ctrl+C / redeploy / restart güvenli; SQLite checkpoint'ten devam eder.
- 429 gelirse script kendisi bekler (60s → 15 dk üstel).
- Sürekli 403 alıyorsan Steam o IP'yi engelliyor demektir → PC'de koştur.
