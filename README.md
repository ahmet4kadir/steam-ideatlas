# Steam Ideatlas

Every Steam game, embedded and mapped. New game ideas live in the empty space between them.

- `crawler/`  — full Steam catalog fetcher (store appdetails + reviews + SteamSpy tags), resumable, Docker/Coolify ready
- `pipeline/` — embeddings, UMAP map, cluster labels, empty-space search
- `api/`      — "what lives between these?": Claude writes a concept, we embed it and measure it against every game, feed back, repeat; ideas gallery
- `web/`      — interactive point-cloud viewer (three.js) with Mix mode, empty-space mode, loners, ideas — ideatlas.ahmetkadir.com
