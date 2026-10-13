# jseries

Organize a TV series library and tag it with TMDB identifiers, for Jellyfin/Plex.
Runs locally on a NAS (e.g. a Synology scheduled task). The id goes on the
**series folder** (renamed in place); episode files inside are tidied in place.

```
Rick and Morty/                 ->  Rick and Morty (2013) [tmdbid-60625]/
  rick.and.morty.s01e01.PL.mkv  ->    Rick and Morty S01E01.PL.mkv
```

## Use

```
python3 organize.py --dir "/volume1/video/Series" --apply
```

- Without `--apply` it only previews (writes a log, changes nothing).
- `--dir` can be repeated; paths may also be listed as `path=` lines in `config.txt`.
- Needs `tmdb_api_key=` in `config.txt` (get a free key at themoviedb.org).

## Behaviour

- Each top-level folder under a path is one show; season/episode markers
  (S01E02, 1x02, E02, Odc 2…) are detected, season taken from `Season XX` if absent.
- Folders that already have an id are still scanned for new episodes. A small
  cache (`jseries_cache.json`) skips a show whose episode count is unchanged.
- A name clash gets a ` - duplicateN` suffix; nothing is moved or deleted.
- Subtitles, `.nfo` and artwork sharing an episode's name are renamed with it.
- Dated logs next to the script: `jseries-YYYY-MM-DD.log` plus one per path;
  `log_keep_days=` prunes old ones. A lock file prevents overlapping runs.

Requires only Python 3 (standard library — no `requests` needed).
