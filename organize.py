#!/usr/bin/env python3
"""
organize.py — organize a TV series library and tag it with TMDB identifiers.

Runs LOCALLY on the NAS (e.g. as a Synology Task Scheduler job). The library
root (--dir) holds one folder per show. A single run does the whole job:

  * the TMDB identifier goes on the SERIES FOLDER, which is renamed in place:
        Rick and Morty  ->  Rick and Morty (2013) [tmdbid-60625]
    (the folder is renamed on disk; its contents are not moved),
  * EPISODE files inside are renamed in place to a tidy pattern, with the
    proper show title and a standardized marker — WITHOUT an id (the id lives
    on the folder):
        rick.and.morty.s01e01.PL.1080p.mkv  ->  Rick and Morty S01E01.PL.1080p.mkv
  * folders that ALREADY carry an id are still scanned (unlike jmovies), so
    newly added episodes get tidied up; the id is then read straight from the
    folder name without querying TMDB,
  * a small cache (keyed by tmdbid) remembers each show's episode count; an
    id-tagged folder whose count has not changed since the last run is skipped
    entirely (no TMDB call, no re-scan). Add one episode (20 -> 21) and the
    folder is scanned again. The cache is written only in --apply runs.
  * if a target name is already taken (collision), nothing is overwritten:
    a " - duplicateN" suffix is added and the event is logged,
  * episode files with no detectable season/episode marker are reported and
    left untouched; nothing is ever deleted,
  * the run (start / progress / finish) is written to a log file and printed
    to the console.

Usage:
  python3 organize.py --dir /volume1/video/Series            # preview
  python3 organize.py --dir /volume1/video/Series --apply    # execute

TMDB key: --api-key flag, TMDB_API_KEY environment variable, or the
tmdb_api_key entry in config.txt located next to the script.
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
import unicodedata

try:
    import requests
except ImportError:
    print('Missing the requests library. Install it: pip3 install requests', file=sys.stderr)
    sys.exit(1)


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_EXT = {'.ts', '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.mpg', '.mpeg', '.m2ts'}

RE_TMDBID = re.compile(r'\[tmdbid-(\d+)\]', re.IGNORECASE)
RE_YEAR_PAREN = re.compile(r'\((?:19|20)\d{2}\)')
RE_YEAR_BARE = re.compile(r'(?<![0-9])(?:19|20)\d{2}(?![0-9])')

# Episode markers:
# S01E01, S01.E01, s01e01, S01 E01
RE_SXE = re.compile(r'[Ss](\d{1,2})[ ._\-]*[Ee](\d{1,2})')
# 1x01 (digit before x, exactly 2 episode digits -> does not catch 1920x1080)
RE_AXB = re.compile(r'(?<!\d)(\d{1,2})[xX](\d{2})(?!\d)')
# bare E01 / EP01 / Ep 1 (lookbehind rejects the "E" of S01E01)
RE_EP = re.compile(r'(?<![A-Za-z0-9])[Ee][Pp]?\s*\.?\s*(\d{1,3})(?![A-Za-z0-9])')
# Część 2 / Cz 2
RE_CZ = re.compile(r'(?<![A-Za-z0-9])(?:CZ|Cz|Część|Czesc|CZĘŚĆ)\s*\.?\s*(\d{1,3})(?![A-Za-z0-9])')
# Part 2 / Pt 2 / Odcinek 2 / Odc 2
RE_PART = re.compile(r'(?<![A-Za-z0-9])(?:Part|Pt|Odcinek|Odc)\s*\.?\s*(\d{1,3})(?![A-Za-z0-9])',
                     re.IGNORECASE)
# Season folder: "Season 1" / "Sezon 01" / "S1"
RE_SEASON_DIR = re.compile(r'^(?:season|sezon)\s*0*(\d{1,2})$', re.IGNORECASE)
RE_SDIR_SHORT = re.compile(r'^s\s*0*(\d{1,2})$', re.IGNORECASE)

# Known release tags — used to split the title from the technical part,
# even when the episode marker sits in the middle (e.g. Show.PL.CZ2.1080i...)
_RELEASE = (r'(?:PL|ENG|EN|MULTi|LEKTOR|NAPISY|NAPiSY|DUB|SUB|2160p|1080p|1080i|720p|720i|'
            r'576p|540p|480p|UHD|UHDTV|HDTV|HDR10|HDR|DV|SDR|WEB-?DL|WEBRip|WEB|BluRay|'
            r'Blu-?Ray|BDRip|BRRip|DVDRip|HDRip|REMUX|HEVC|AVC|x264|x265|h264|h265|H\.264|'
            r'H\.265|DDP?5\.1|DD\+|E?AC-?3|AAC(?:2\.0)?|DTS(?:-HD)?(?:\.MA)?|TrueHD|Atmos|'
            r'FLAC|MA|5\.1|2\.0|B89|MR)')
RE_RELEASE_TOKEN = re.compile(r'(?<![A-Za-z0-9])' + _RELEASE + r'(?![A-Za-z0-9])', re.IGNORECASE)

# Release words made of plain letters (to tell them apart from an episode TITLE).
RELEASE_WORDS = {
    'pl', 'eng', 'en', 'multi', 'lektor', 'napisy', 'dub', 'sub', 'uhd', 'uhdtv',
    'hdtv', 'hdr', 'hdr10', 'dv', 'sdr', 'web', 'webrip', 'webdl', 'dl', 'bluray',
    'bdrip', 'brrip', 'dvdrip', 'hdrip', 'remux', 'hevc', 'avc', 'truehd', 'atmos',
    'flac', 'aac', 'dts', 'ddp', 'ac', 'eac', 'proper', 'repack', 'internal',
    'extended', 'remastered', 'uncut', 'hybrid', 'imax', 'dual', 'dubbed', 'subbed',
    'ma', 'mr', 'hd', 'sd',
}

log = logging.getLogger('jseries')


# --------------------------- configuration ---------------------------

def read_config(path):
    cfg = {}
    if not os.path.isfile(path):
        return cfg
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            cfg[k.strip()] = v.strip()
    return cfg


def config_paths(path):
    """Return every 'path=' entry from the config, in order (one path per line).
    Paths may contain spaces and need no quoting inside the config file."""
    out = []
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            if k.strip() == 'path' and v.strip():
                out.append(v.strip())
    return out


def load_cache(path):
    """Load the scan cache: {tmdbid(str): {'name','year','count'}}."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(v, dict) and isinstance(v.get('count'), int)}


def save_cache(path, cache):
    if not path:
        return
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(cache, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning('Could not write cache (%s): %s', path, exc)


# --------------------------- text / names ---------------------------

def normalize(s):
    s = unicodedata.normalize('NFKD', s)
    s = ''.join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r'[^a-z0-9]+', ' ', s)
    return re.sub(r'\s{2,}', ' ', s).strip()


def sanitize_name(name):
    s = re.sub(r'[<>:"/\\|?*]', ' ', name)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' .')


def clean_query(raw):
    s = RE_TMDBID.sub('', raw)
    s = re.sub(r'[._]+', ' ', s)
    s = RE_YEAR_PAREN.sub('', s)
    s = RE_YEAR_BARE.sub('', s)
    s = re.sub(r'\[[^\]]*\]', '', s)
    s = re.sub(r'\s{2,}', ' ', s)
    return s.strip(' -')


def year_from_text(s):
    m = RE_YEAR_PAREN.search(s)
    if m:
        return m.group(0)[1:5]
    m = RE_YEAR_BARE.search(s)
    return m.group(0) if m else None


def strip_sep(s):
    return s.strip(' ._-')


def looks_like_title(tail):
    """True if the text after the marker is an episode TITLE (e.g. 'Ice Cream')
    rather than release tags — i.e. it contains a plain word (letters only, >=3)
    that is not in the release-tag dictionary."""
    if not tail:
        return False
    for t in re.split(r'[.\s_\-]+', tail):
        if t and re.fullmatch(r'[^\W\d_]{3,}', t) and normalize(t) not in RELEASE_WORDS:
            return True
    return False


def find_marker(stem):
    """Return (start, end, season_or_None, episode) of the earliest marker."""
    cands = []
    m = RE_SXE.search(stem)
    if m:
        cands.append((m.start(), m.end(), int(m.group(1)), int(m.group(2))))
    m = RE_AXB.search(stem)
    if m:
        cands.append((m.start(), m.end(), int(m.group(1)), int(m.group(2))))
    for pat in (RE_EP, RE_CZ, RE_PART):
        m = pat.search(stem)
        if m:
            cands.append((m.start(), m.end(), None, int(m.group(1))))
    if not cands:
        return None
    return min(cands, key=lambda c: c[0])


def parse_file(base, season_hint):
    """Return (season, episode, title_from_file, release, ext) or None."""
    stem, ext = os.path.splitext(base)
    mk = find_marker(stem)
    if not mk:
        return None
    start, end, season, episode = mk
    if season is None:
        season = season_hint if season_hint else 1

    before = stem[:start]
    after = strip_sep(stem[end:])

    # Drop the episode title after the marker (e.g. ".Ice Cream", "_Denim"),
    # keeping only release tags. This way duplicates of the same episode get
    # an identical name instead of multiplying.
    if looks_like_title(after):
        after = ''

    # If release tags already precede the marker (e.g. "PL"), keep them.
    rel_m = RE_RELEASE_TOKEN.search(before)
    if rel_m:
        pre_rel = strip_sep(before[rel_m.start():])
        title_part = before[:rel_m.start()]
    else:
        pre_rel = ''
        title_part = before

    release = '.'.join(p for p in (pre_rel, after) if p)
    return season, episode, clean_query(title_part), release, ext


def season_from_path(ancestors):
    for part in ancestors:
        m = RE_SEASON_DIR.match(part.strip()) or RE_SDIR_SHORT.match(part.strip())
        if m:
            return int(m.group(1))
    return None


# --------------------------- TMDB ---------------------------

class Tmdb:
    def __init__(self, key, language):
        self.language = language
        self.session = requests.Session()
        self.v3 = None
        if key.count('.') >= 2 and len(key) > 50:
            self.session.headers['Authorization'] = f'Bearer {key}'
        else:
            self.v3 = key

    def _params(self, extra=None):
        p = {'language': self.language}
        if self.v3:
            p['api_key'] = self.v3
        if extra:
            p.update(extra)
        return p

    def search_tv(self, query):
        r = self.session.get('https://api.themoviedb.org/3/search/tv',
                             params=self._params({'query': query, 'include_adult': 'false'}),
                             timeout=25)
        r.raise_for_status()
        return r.json().get('results', [])

    def get_tv(self, tv_id):
        r = self.session.get(f'https://api.themoviedb.org/3/tv/{tv_id}',
                             params=self._params(), timeout=25)
        r.raise_for_status()
        return r.json()


def _name(r):
    return r.get('name') or r.get('original_name') or ''


def _yr(r):
    return (r.get('first_air_date') or '')[:4]


def to_result(data):
    return {'ok': True, 'id': data.get('id'),
            'name': sanitize_name(_name(data)),
            'year': _yr(data)}


def pick_candidate(results, nquery, hint_year):
    if not results:
        return None
    exact = [r for r in results
             if normalize(r.get('name', '')) == nquery
             or normalize(r.get('original_name', '')) == nquery]
    if hint_year and hint_year.isdigit():
        hy = int(hint_year)
        ey = [r for r in exact if _yr(r) == hint_year]
        if len(ey) == 1:
            return ey[0]
        ay = [r for r in results if _yr(r) == hint_year]
        if len(ay) == 1:
            return ay[0]
        withy = [r for r in exact if _yr(r).isdigit()]
        if withy:
            withy.sort(key=lambda r: abs(int(_yr(r)) - hy))
            if len(withy) == 1 or abs(int(_yr(withy[0])) - hy) < abs(int(_yr(withy[1])) - hy):
                return withy[0]
    if len(exact) == 1:
        return exact[0]
    if len(results) == 1:
        return results[0]
    return None


def resolve_series(tmdb, query, tmdbid_known, hint_year):
    """Return {'ok':True,'id','name','year'} or {'ok':False,'reason'}."""
    try:
        if tmdbid_known:
            return to_result(tmdb.get_tv(tmdbid_known))
        nq = normalize(query)
        results = tmdb.search_tv(query)
        chosen = pick_candidate(results, nq, hint_year)
        cand_source = results
        if not chosen and not results:
            words = query.split()
            for cut in range(len(words) - 1, 1, -1):
                r2 = tmdb.search_tv(' '.join(words[:cut]))
                if not r2:
                    continue
                cand_source = r2
                nq2 = normalize(' '.join(words[:cut]))
                exact = [x for x in r2
                         if normalize(_name(x)) == nq2]
                if exact:
                    chosen = pick_candidate(exact, nq2, hint_year) or exact[0]
                    break
        if chosen:
            return to_result(chosen)
        reason = ('no TMDB results' if not cand_source
                  else f'{len(cand_source)} results, no unambiguous match')
        return {'ok': False, 'reason': reason}
    except requests.RequestException as exc:
        return {'ok': False, 'reason': f'TMDB error: {exc}'}


# --------------------------- logging ---------------------------

LOG_FMT = logging.Formatter('%(asctime)s  %(levelname)s  %(message)s', '%Y-%m-%d %H:%M:%S')


def setup_console():
    log.setLevel(logging.INFO)
    log.handlers.clear()
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(LOG_FMT)
    log.addHandler(ch)


def add_file_handler(log_dir, name):
    """Attach a file handler <log_dir>/<name>; return it (or None on failure)."""
    if not log_dir:
        return None
    try:
        os.makedirs(log_dir, exist_ok=True)
        fh = logging.FileHandler(os.path.join(log_dir, name), encoding='utf-8')
        fh.setFormatter(LOG_FMT)
        log.addHandler(fh)
        return fh
    except OSError as exc:
        log.warning('Could not open log file (%s): %s', os.path.join(log_dir, name), exc)
        return None


def drop_handler(fh):
    if fh:
        log.removeHandler(fh)
        fh.close()


# --------------------------- disk helpers ---------------------------

def free_name(dirn, new_base, claimed):
    """Return a path under dirn for new_base; if taken, append ' - duplicateN'.
    Returns (dest_path, dup_index) where dup_index == 0 means no collision."""
    dest = os.path.join(dirn, new_base)
    if dest not in claimed and not os.path.exists(dest):
        return dest, 0
    stem, ext = os.path.splitext(new_base)
    n = 1
    while True:
        cand = os.path.join(dirn, f'{stem} - duplicate{n}{ext}')
        if cand not in claimed and not os.path.exists(cand):
            return cand, n
        n += 1


def video_files_under(folder):
    """All video files under folder (any depth), skipping Synology's @eaDir."""
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d != '@eaDir']
        for f in filenames:
            if os.path.splitext(f)[1].lower() in VIDEO_EXT:
                out.append(os.path.join(dirpath, f))
    out.sort()
    return out


# --------------------------- main flow ---------------------------

class Stats:
    def __init__(self):
        self.folders_renamed = self.files_renamed = 0
        self.duplicates = self.no_episode = self.unresolved = self.errors = 0
        self.skipped_unchanged = 0


def rename_episodes(show_name, folder_path, apply, stats):
    """Rename every episode file under folder_path to '<show> SxxEyy.release.ext'
    in place (within its own directory). Files already matching are left alone."""
    claimed = set()
    for src in video_files_under(folder_path):
        base = os.path.basename(src)
        dirn = os.path.dirname(src)
        season_hint = season_from_path(src[len(folder_path):].strip('/').split('/')[:-1])
        parsed = parse_file(base, season_hint)
        if not parsed:
            stats.no_episode += 1
            log.warning('    NO EPISODE MARKER: %s', base)
            continue
        season, episode, _title, release, ext = parsed
        new_base = f'{show_name} S{season:02d}E{episode:02d}' + (f'.{release}' if release else '') + ext

        if os.path.join(dirn, new_base) == src:
            continue  # already named correctly

        dest, dup_n = free_name(dirn, new_base, claimed)
        if dup_n:
            stats.duplicates += 1
            new_base = os.path.basename(dest)
            log.warning('    DUPLICATE: "%s" — name taken, assigning "%s"', base, new_base)
        claimed.add(dest)
        if apply:
            try:
                os.rename(src, dest)
                stats.files_renamed += 1
                log.info('    OK: %s  ->  %s', base, new_base)
            except OSError as exc:
                stats.errors += 1
                log.error('    RENAME ERROR "%s": %s', base, exc)
        else:
            stats.files_renamed += 1
            log.info('    RENAME: %s  ->  %s', base, new_base)


def process_root(root, tmdb, apply, cache, cache_path, folder_claimed, stats):
    """Tag and tidy every show folder under one library root, in place."""
    series = sorted(d for d in os.listdir(root)
                    if d != '@eaDir' and os.path.isdir(os.path.join(root, d)))
    total = len(series)
    log.info('---- path: %s  (%d series folders) ----', root, total)

    for i, folder in enumerate(series, 1):
        cur_path = os.path.join(root, folder)
        count = len(video_files_under(cur_path))

        # Cache shortcut: a folder that already carries an id and whose video-file
        # count is unchanged since last time is skipped (and TMDB is not queried).
        m = RE_TMDBID.search(folder)
        if m and cache_path:
            cached = cache.get(str(int(m.group(1))))
            if cached and cached.get('count') == count:
                stats.skipped_unchanged += 1
                log.info('[%d/%d] UNCHANGED (%d episodes): %s/', i, total, count, folder)
                continue

        # Id read straight from the folder name (re-scan case) or resolved by title.
        if m:
            res = resolve_series(tmdb, '', int(m.group(1)), None)
        else:
            res = resolve_series(tmdb, clean_query(folder), None, year_from_text(folder))

        if not res.get('ok'):
            stats.unresolved += 1
            log.warning('[%d/%d] NO MATCH: %s/ — %s', i, total, folder, res.get('reason', '?'))
            continue

        name, yr, tid = res['name'], res['year'], res['id']
        new_folder = f'{name} ({yr}) [tmdbid-{tid}]' if yr else f'{name} [tmdbid-{tid}]'

        # Rename the series folder in place (contents stay inside it).
        if folder != new_folder:
            dest, dup_n = free_name(root, new_folder, folder_claimed)
            if dup_n:
                stats.duplicates += 1
                new_folder = os.path.basename(dest)
                log.warning('[%d/%d] DUPLICATE FOLDER: "%s/" — name taken, assigning "%s/"',
                            i, total, folder, new_folder)
            folder_claimed.add(dest)
            if apply:
                try:
                    os.rename(cur_path, dest)
                    cur_path = dest
                    stats.folders_renamed += 1
                    log.info('[%d/%d] FOLDER: %s/  ->  %s/', i, total, folder, new_folder)
                except OSError as exc:
                    stats.errors += 1
                    log.error('[%d/%d] FOLDER RENAME ERROR "%s/": %s', i, total, folder, exc)
                    continue
            else:
                log.info('[%d/%d] FOLDER: %s/  ->  %s/', i, total, folder, new_folder)
        else:
            folder_claimed.add(cur_path)
            log.info('[%d/%d] FOLDER OK: %s/', i, total, folder)

        # Tidy the episode files inside (in place), using the proper show title.
        rename_episodes(name, cur_path, apply, stats)

        # Remember this show's episode count so an unchanged folder is skipped next run.
        # (Only in apply mode — a preview does not rename, so nothing is settled yet.)
        if cache_path and apply:
            cache[str(tid)] = {'name': name, 'year': yr, 'count': count}


def main():
    ap = argparse.ArgumentParser(
        description='Organize a TV series library and tag folders with TMDB ids (local).')
    ap.add_argument('paths', nargs='*', help='series library directory/directories (positional)')
    ap.add_argument('--dir', '--path', dest='dirs', action='append', default=[], metavar='DIR',
                    help='series library directory; repeat for several (e.g. --dir A --dir B)')
    ap.add_argument('--config', default=os.path.join(SCRIPT_DIR, 'config.txt'))
    ap.add_argument('--apply', action='store_true', help='execute changes (preview by default)')
    ap.add_argument('--dry-run', action='store_true', help='force preview')
    ap.add_argument('--api-key', default=None)
    ap.add_argument('--language', default=None)
    ap.add_argument('--log-dir', default=None,
                    help='directory for dated log files (default: next to the script)')
    ap.add_argument('--no-log', action='store_true', help='do not write log files')
    ap.add_argument('--cache', default=None, help='scan cache path (default: jseries_cache.json)')
    ap.add_argument('--no-cache', action='store_true',
                    help='ignore the cache and rescan every folder')
    args = ap.parse_args()

    cfg = read_config(args.config)

    given = args.dirs + args.paths or config_paths(args.config)
    api_key = args.api_key or os.environ.get('TMDB_API_KEY') or cfg.get('tmdb_api_key')
    language = args.language or cfg.get('tmdb_language') or 'en-US'
    apply = args.apply or cfg.get('mode', 'preview').lower().startswith('apply')
    if args.dry_run:
        apply = False
    log_dir = None if args.no_log else (args.log_dir or cfg.get('log_dir') or SCRIPT_DIR)
    cache_path = None if args.no_cache else (args.cache or cfg.get('cache_path')
                                             or os.path.join(SCRIPT_DIR, 'jseries_cache.json'))
    datestr = datetime.date.today().isoformat()

    setup_console()
    main_log = add_file_handler(log_dir, f'jseries-{datestr}.log')

    if not given:
        log.error('No library directory. Pass --dir /path (or set path= in config.txt).')
        sys.exit(1)
    if not api_key or api_key.startswith('PUT_YOUR'):
        log.error('No TMDB key (--api-key / TMDB_API_KEY / tmdb_api_key in config.txt).')
        sys.exit(1)

    # Keep existing directories only; drop duplicates / repeats by realpath.
    seen, roots = set(), []
    for p in given:
        rp = os.path.realpath(p.rstrip('/'))
        if not os.path.isdir(rp):
            log.error('Not a directory, skipping: %s', p)
            continue
        if rp not in seen:
            seen.add(rp)
            roots.append(rp)
    if not roots:
        log.error('None of the given paths is a directory.')
        sys.exit(1)

    tmdb = Tmdb(api_key, language)
    cache = {} if not cache_path else load_cache(cache_path)
    stats = Stats()
    folder_claimed = set()

    log.info('==== START ==== %d path(s)', len(roots))
    for i, root in enumerate(roots, 1):
        log.info('  path%d = %s', i, root)
    log.info('Mode: %s', 'APPLY' if apply else 'PREVIEW (no changes, add --apply)')
    if cache_path:
        log.info('Cache: %s (%d shows known)', cache_path, len(cache))

    # Each path is processed separately and also gets its own dated log file,
    # named by its index (path1, path2, ...). The full path is logged inside.
    for i, root in enumerate(roots, 1):
        path_log = add_file_handler(log_dir, f'jseries-path{i}-{datestr}.log')
        process_root(root, tmdb, apply, cache, cache_path, folder_claimed, stats)
        drop_handler(path_log)

    if cache_path and apply:
        save_cache(cache_path, cache)

    log.info('==== DONE ====')
    fverb = 'Folders renamed' if apply else 'Folders to rename'
    everb = 'episodes renamed' if apply else 'episodes to rename'
    log.info('%s: %d | %s: %d | unchanged (skipped by cache): %d | suffixed duplicates: %d | '
             'no episode marker: %d | folders without match: %d | errors: %d',
             fverb, stats.folders_renamed, everb, stats.files_renamed,
             stats.skipped_unchanged, stats.duplicates,
             stats.no_episode, stats.unresolved, stats.errors)
    if not apply:
        log.info('This was a PREVIEW — add --apply to execute changes.')
    drop_handler(main_log)


if __name__ == '__main__':
    main()