#!/usr/bin/env python3
"""
jellyfin_porzadkuj.py

Porządkuje bibliotekę seriali dokumentalnych pod Jellyfin, operując na NAS-ie
po SSH (find + mkdir -p + mv; bez SFTP). Dopasowania TMDB liczone są lokalnie.

Program NIE modyfikuje zawartości plików — wykonuje wyłącznie:
  - tworzenie katalogów (mkdir -p),
  - przenoszenie i zmianę nazw plików (mv).

Docelowa struktura:
  Nazwa serialu (ROK) [tmdbid-XXXXX]/
    Season 01/
      Nazwa serialu S01E01.PL.1080i.HDTV.H264-B89.ts

Domyślnie działa w trybie PODGLĄDU. Faktyczne zmiany: flaga --apply.
"""

import argparse
import datetime
import json
import os
import re
import shlex
import sys
import unicodedata

try:
    import paramiko
except ImportError:
    print('Brak biblioteki paramiko. Uruchom przez ./uruchom_jellyfin.sh', file=sys.stderr)
    sys.exit(1)
try:
    import requests
except ImportError:
    print('Brak biblioteki requests. Uruchom przez ./uruchom_jellyfin.sh', file=sys.stderr)
    sys.exit(1)


VIDEO_EXT = {'.ts', '.mkv', '.mp4', '.avi', '.m4v', '.mov', '.wmv', '.mpg', '.mpeg', '.m2ts'}

# --- Rozpoznawanie sezonu/odcinka ---
# S01E01, S01.E01, s01e01, S01 E01
RE_SXE = re.compile(r'[Ss](\d{1,2})[ ._\-]*[Ee](\d{1,2})')
# 1x01  (wymaga cyfry przed x i dokładnie 2 cyfr odcinka -> nie łapie 1920x1080)
RE_AXB = re.compile(r'(?<!\d)(\d{1,2})[xX](\d{2})(?!\d)')
# Samo E01 / EP01 / Ep 1  (lookbehind odrzuca "E" z S01E01)
RE_EP = re.compile(r'(?<![A-Za-z0-9])[Ee][Pp]?\s*\.?\s*(\d{1,3})(?![A-Za-z0-9])')
# Polskie CZ1 / Cz 2 / Część 3
RE_CZ = re.compile(r'(?<![A-Za-z0-9])(?:CZ|Cz|Część|Czesc|CZĘŚĆ)\s*\.?\s*(\d{1,3})(?![A-Za-z0-9])')
# Part 2 / Pt 2 / Odcinek 2 / Odc 2
RE_PART = re.compile(r'(?<![A-Za-z0-9])(?:Part|Pt|Odcinek|Odc)\s*\.?\s*(\d{1,3})(?![A-Za-z0-9])',
                     re.IGNORECASE)

RE_TMDBID = re.compile(r'\[tmdbid-(\d+)\]', re.IGNORECASE)
RE_SEASON_DIR = re.compile(r'^(?:season|sezon)\s*0*(\d{1,2})$', re.IGNORECASE)
RE_SDIR_SHORT = re.compile(r'^s\s*0*(\d{1,2})$', re.IGNORECASE)
RE_YEAR_PAREN = re.compile(r'\((?:19|20)\d{2}\)')
RE_YEAR_BARE = re.compile(r'(?<![0-9])(?:19|20)\d{2}(?![0-9])')

# Znane tagi release — używane do odcięcia tytułu od części technicznej,
# także gdy znacznik odcinka stoi w środku (np. Singapur.PL.CZ2.1080i...)
_RELEASE = (r'(?:PL|ENG|EN|MULTi|LEKTOR|NAPISY|NAPiSY|DUB|SUB|2160p|1080p|1080i|720p|720i|'
            r'576p|540p|480p|UHD|UHDTV|HDTV|HDR10|HDR|DV|SDR|WEB-?DL|WEBRip|WEB|BluRay|'
            r'Blu-?Ray|BDRip|BRRip|DVDRip|HDRip|REMUX|HEVC|AVC|x264|x265|h264|h265|H\.264|'
            r'H\.265|DDP?5\.1|DD\+|E?AC-?3|AAC(?:2\.0)?|DTS(?:-HD)?(?:\.MA)?|TrueHD|Atmos|'
            r'FLAC|MA|5\.1|2\.0|B89|MR)')
RE_RELEASE_TOKEN = re.compile(r'(?<![A-Za-z0-9])' + _RELEASE + r'(?![A-Za-z0-9])', re.IGNORECASE)

# Słowa release będące samymi literami (do odróżnienia od TYTUŁU odcinka).
RELEASE_WORDS = {
    'pl', 'eng', 'en', 'multi', 'lektor', 'napisy', 'dub', 'sub', 'uhd', 'uhdtv',
    'hdtv', 'hdr', 'hdr10', 'dv', 'sdr', 'web', 'webrip', 'webdl', 'dl', 'bluray',
    'bdrip', 'brrip', 'dvdrip', 'hdrip', 'remux', 'hevc', 'avc', 'truehd', 'atmos',
    'flac', 'aac', 'dts', 'ddp', 'ac', 'eac', 'proper', 'repack', 'internal',
    'extended', 'remastered', 'uncut', 'hybrid', 'imax', 'dual', 'dubbed', 'subbed',
    'ma', 'mr', 'hd', 'sd',
}


# --------------------------- konfiguracja ---------------------------

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


def load_cache(path):
    """Wczytuje trwały cache dopasowań TMDB (tylko udane wpisy)."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get('ok')}


def save_cache(path, cache):
    """Zapisuje do cache tylko rozwiązane (udane) dopasowania."""
    if not path:
        return
    ok = {k: v for k, v in cache.items() if isinstance(v, dict) and v.get('ok')}
    try:
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(ok, fh, ensure_ascii=False, indent=0, sort_keys=True)
        os.replace(tmp, path)
    except OSError as exc:
        print(f'Uwaga: nie udało się zapisać cache ({exc}).', file=sys.stderr)


def write_log(path, root, apply, plan, moved, skipped_exist, apply_output,
              collisions, skipped_files, unresolved):
    """Dopisuje wpis o przebiegu do pliku logów."""
    if not path:
        return
    ts = datetime.datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
    lines = ['=' * 60,
             f'{ts}  tryb={"WYKONANIE" if apply else "PODGLĄD"}  biblioteka={root}',
             f'Zaplanowano zmian: {len(plan)}']
    for src, dest in plan:
        lines.append(f'  {src}')
        lines.append(f'    -> {dest}')
    if apply:
        lines.append(f'Wynik: przeniesiono {moved}, pominięto (istniały) {skipped_exist}')
        for ln in (apply_output or '').splitlines():
            lines.append('  ' + ln)
    if collisions:
        lines.append(f'Kolizje: {len(collisions)}')
        for src, other, dest in collisions:
            lines.append(f'  {src}')
            lines.append(f'  ORAZ {other}')
            lines.append(f'  -> {dest}')
    if skipped_files:
        lines.append(f'Pominięte pliki: {len(skipped_files)}')
        for full, why in skipped_files:
            lines.append(f'  [{why}] {full}')
    if unresolved:
        lines.append(f'Bez jednoznacznego dopasowania TMDB: {len(unresolved)}')
        for query, hint, reason, cands, nfiles in unresolved:
            hint_s = f', rok z folderu: {hint}' if hint else ''
            lines.append(f'  "{query}" — {reason}{hint_s} ({nfiles} plik(ów))')
            for c in cands:
                lines.append(f'      {c["name"]} ({c["year"]}) tmdbid-{c["id"]}')
    lines.append('')
    try:
        with open(path, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    except OSError as exc:
        print(f'Uwaga: nie udało się zapisać logu ({exc}).', file=sys.stderr)


def load_overrides(path):
    out = {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            left, right = line.split('=', 1)
            right = right.strip()
            if right.isdigit():
                out[normalize(left.strip())] = int(right)
    return out


# --------------------------- tekst / nazwy ---------------------------

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
    s = re.sub(r'(?<![A-Za-z0-9])(?:19|20)\d{2}(?![A-Za-z0-9])', '', s)
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
    """True, jeśli tekst po znaczniku to TYTUŁ odcinka (np. 'Ice Cream', 'Lody'),
    a nie tagi release — czyli zawiera zwykłe słowo (same litery, >=3), którego
    nie ma w słowniku tagów release."""
    if not tail:
        return False
    for t in re.split(r'[.\s_\-]+', tail):
        if t and re.fullmatch(r'[^\W\d_]{3,}', t) and normalize(t) not in RELEASE_WORDS:
            return True
    return False


def find_marker(stem):
    """Zwraca (start, end, sezon_lub_None, odcinek) najwcześniejszego znacznika."""
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
    """Zwraca (sezon, odcinek, tytul_z_pliku, czesc_release, ext) albo None."""
    stem, ext = os.path.splitext(base)
    mk = find_marker(stem)
    if not mk:
        return None
    start, end, season, episode = mk
    if season is None:
        season = season_hint if season_hint else 1

    before = stem[:start]
    after = strip_sep(stem[end:])

    # Odrzuć tytuł odcinka po znaczniku (np. ".Ice Cream", ".Lody", "_Denim"),
    # zachowując tylko tagi release. Dzięki temu duplikaty tego samego odcinka
    # dostają identyczną nazwę i nie mnożą się w folderze.
    if looks_like_title(after):
        after = ''

    # Jeśli przed znacznikiem są już tagi release (np. "PL"), dołącz je do części technicznej
    rel_m = RE_RELEASE_TOKEN.search(before)
    if rel_m:
        pre_rel = strip_sep(before[rel_m.start():])
        title_part = before[:rel_m.start()]
    else:
        pre_rel = ''
        title_part = before

    rel_parts = [p for p in (pre_rel, after) if p]
    release = '.'.join(rel_parts)
    return season, episode, clean_query(title_part), release, ext


def parse_unnumbered(base):
    """Dla pliku bez numeru: dzieli nazwę na tytuł i część release po pierwszym
    znanym tagu release. Zwraca (tytul, release, ext)."""
    stem, ext = os.path.splitext(base)
    m = RE_RELEASE_TOKEN.search(stem)
    if m:
        title = stem[:m.start()]
        release = strip_sep(stem[m.start():])
    else:
        title = stem
        release = ''
    return clean_query(title), release, ext


def season_from_path(ancestors):
    for part in ancestors:
        p = part.strip()
        m = RE_SEASON_DIR.match(p) or RE_SDIR_SHORT.match(p)
        if m:
            return int(m.group(1))
    return None


def series_source_name(ancestors, title_from_file):
    for part in ancestors:
        p = part.strip()
        if RE_SEASON_DIR.match(p) or RE_SDIR_SHORT.match(p):
            continue
        q = clean_query(part)
        if q:
            return q
    return title_from_file


def series_year_hint(ancestors, title_raw):
    for part in ancestors:
        p = part.strip()
        if RE_SEASON_DIR.match(p) or RE_SDIR_SHORT.match(p):
            continue
        y = year_from_text(part)
        if y:
            return y
    return year_from_text(title_raw or '')


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


def to_result(data):
    name = data.get('name') or data.get('original_name') or ''
    return {'ok': True, 'id': data.get('id'),
            'name': sanitize_name(name),
            'year': (data.get('first_air_date') or '')[:4]}


def _yr(r):
    return (r.get('first_air_date') or '')[:4]


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
        # Przy kilku dokładnych nazwach wybierz rok najbliższy folderowemu
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


def resolve_series(tmdb, query, tmdbid_known, hint_year, cache, overrides):
    nq = normalize(query)
    # Override ma pierwszeństwo nad cache zapytań (by nowy wpis nadpisał stary wynik)
    if not tmdbid_known and nq in overrides:
        okey = f'ov:{overrides[nq]}'
        if okey in cache:
            return cache[okey]
        try:
            res = to_result(tmdb.get_tv(overrides[nq]))
        except requests.RequestException as exc:
            res = {'ok': False, 'reason': f'błąd TMDB: {exc}', 'candidates': []}
        cache[okey] = res
        return res

    key = f'id:{tmdbid_known}' if tmdbid_known else f'q:{nq}|{hint_year}'
    if key in cache:
        return cache[key]
    try:
        if tmdbid_known:
            res = to_result(tmdb.get_tv(tmdbid_known))
        else:
            results = tmdb.search_tv(query)
            chosen = pick_candidate(results, normalize(query), hint_year)
            cand_source = results
            # Gdy pełna nazwa nic nie zwraca, spróbuj krótszych wariantów
            # (ucinanie końcowych słów), ale akceptuj tylko dokładne dopasowanie nazwy.
            if not chosen and not results:
                words = query.split()
                for cut in range(len(words) - 1, 1, -1):
                    q = ' '.join(words[:cut])
                    r2 = tmdb.search_tv(q)
                    if not r2:
                        continue
                    cand_source = r2
                    nq = normalize(q)
                    exact = [x for x in r2
                             if normalize(x.get('name', '')) == nq
                             or normalize(x.get('original_name', '')) == nq]
                    if exact:
                        chosen = pick_candidate(exact, nq, hint_year) or exact[0]
                        break
            if chosen:
                res = to_result(chosen)
            else:
                if not cand_source:
                    reason = 'brak wyników TMDB'
                else:
                    reason = f'{len(cand_source)} wyników, brak jednoznacznego'
                cands = [{'id': r.get('id'), 'name': r.get('name'), 'year': _yr(r)}
                         for r in cand_source[:5]]
                res = {'ok': False, 'reason': reason, 'candidates': cands}
    except requests.RequestException as exc:
        res = {'ok': False, 'reason': f'błąd TMDB: {exc}', 'candidates': []}
    cache[key] = res
    return res


# --------------------------- sprzątanie ---------------------------

def build_cleanup_lines(root, keep_vsmeta=False):
    """Polecenia powłoki: skasuj WSZYSTKIE katalogi w bibliotece, w których poza
    śmieciami nic nie ma — od dołu do góry (także zagnieżdżone). Katalog z plikiem
    wideo lub inną realną zawartością zostaje nietknięty.

    Za śmieci uznaje: @eaDir (Synology), DS_Store/.DS_Store, ._* (macOS),
    Thumbs.db, desktop.ini, @SynoResource, .@__thumb oraz (domyślnie) pliki
    .vsmeta (metadane Synology Video Station, zbędne dla Jellyfin)."""
    qroot = shlex.quote(root)
    junk = r'@eaDir|\.?DS_Store|\._.*|Thumbs\.db|@SynoResource|\.@__thumb|desktop\.ini'
    lines = []
    if not keep_vsmeta:
        junk += r'|.*\.vsmeta'
        # Najpierw usuń WSZYSTKIE pliki .vsmeta w całej bibliotece (także tam, gdzie są filmy)
        lines += [
            f"VSN=$(find {qroot} -type f -name '*.vsmeta' 2>/dev/null | wc -l)",
            f"find {qroot} -type f -name '*.vsmeta' -exec rm -f -- {{}} + 2>/dev/null",
            "printf 'USUNIETO VSMETA: %s\\n' \"$VSN\"",
        ]
    lines += [
        f"JUNK={shlex.quote(junk)}",
        f"find {qroot} -depth -type d 2>/dev/null | while IFS= read -r d; do",
        f"  [ \"$d\" = {qroot} ] && continue",
        "  case \"$d\" in */@eaDir|*/@eaDir/*) continue;; esac",
        "  if [ -z \"$(ls -A \"$d\" 2>/dev/null | grep -vxE \"$JUNK\")\" ]; then",
        "    rm -rf -- \"$d\" 2>/dev/null && printf 'USUNIETO KATALOG: %s\\n' \"$d\"",
        "  fi",
        "done",
    ]
    return lines


# --------------------------- SSH ---------------------------

def ssh_run(client, command, stdin_data=None):
    sin, sout, serr = client.exec_command(command)
    if stdin_data is not None:
        sin.write(stdin_data)
        sin.channel.shutdown_write()
    out = sout.read().decode('utf-8', 'replace')
    err = serr.read().decode('utf-8', 'replace')
    code = sout.channel.recv_exit_status()
    return code, out, err


# --------------------------- główny przebieg ---------------------------

def main():
    ap = argparse.ArgumentParser(description='Porządkowanie seriali pod Jellyfin (SSH + TMDB).')
    ap.add_argument('--config', default='konfiguracja_jellyfin.txt')
    ap.add_argument('--overrides', default='tmdb_overrides.txt')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--api-key', default=None)
    ap.add_argument('--language', default=None)
    ap.add_argument('--path', default=None)
    ap.add_argument('--cleanup-empty', action='store_true',
                    help='(domyślnie włączone przy --apply) usuwa puste stare katalogi')
    ap.add_argument('--no-cleanup-empty', action='store_true',
                    help='nie usuwaj pustych katalogów po przeniesieniu')
    ap.add_argument('--cleanup-only', action='store_true',
                    help='tylko usuń puste katalogi w bibliotece (bez TMDB i przenoszenia)')
    ap.add_argument('--keep-vsmeta', action='store_true',
                    help='nie traktuj plików .vsmeta jako śmieci (zachowaj je)')
    ap.add_argument('--dedupe-only', action='store_true',
                    help='usuń duplikaty tego samego odcinka (zostaje największy plik); '
                         'bez --apply tylko pokazuje')
    ap.add_argument('--single-as-e01', action='store_true',
                    help='folder z JEDNYM nierozpoznanym plikiem -> S01E01')
    ap.add_argument('--cache', default=None,
                    help='plik cache dopasowań TMDB (domyślnie tmdb_cache.json)')
    ap.add_argument('--no-cache', action='store_true', help='nie używaj cache')
    ap.add_argument('--log', default=None,
                    help='plik logów przebiegów (domyślnie jellyfin.log)')
    ap.add_argument('--no-log', action='store_true', help='nie zapisuj logów')
    args = ap.parse_args()

    cfg = read_config(args.config)
    for req in ('host', 'user', 'password'):
        if not cfg.get(req):
            print(f'Brak "{req}" w {args.config}', file=sys.stderr)
            sys.exit(1)

    root = (args.path or cfg.get('path') or '').rstrip('/')
    if not root:
        print('Brak ścieżki biblioteki (path).', file=sys.stderr)
        sys.exit(1)

    api_key = args.api_key or os.environ.get('TMDB_API_KEY') or cfg.get('tmdb_api_key')
    if not args.cleanup_only and (not api_key or api_key.startswith('TU_WPISZ')):
        print('Brak klucza TMDB (--api-key / TMDB_API_KEY / tmdb_api_key w konfiguracji).',
              file=sys.stderr)
        sys.exit(1)

    language = args.language or cfg.get('tmdb_language') or 'en-US'
    apply = args.apply or cfg.get('tryb', 'podglad').lower().startswith('wykonaj')
    if args.dry_run:
        apply = False

    overrides = load_overrides(args.overrides)
    tmdb = Tmdb(api_key, language) if api_key else None

    cache_path = None if args.no_cache else (args.cache or cfg.get('cache_path') or 'tmdb_cache.json')
    log_path = None if args.no_log else (args.log or cfg.get('log_path') or 'jellyfin.log')

    print(f"Łączę: {cfg['user']}@{cfg['host']}:{cfg.get('port', '22')}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(cfg['host'], port=int(cfg.get('port', '22')),
                       username=cfg['user'], password=cfg['password'],
                       look_for_keys=False, allow_agent=False, timeout=20)
    except Exception as exc:  # noqa: BLE001
        print(f'Nie udało się połączyć: {exc}', file=sys.stderr)
        sys.exit(1)

    code, _o, _e = ssh_run(client, f'test -d {shlex.quote(root)}')
    if code != 0:
        print(f'Ścieżka nie istnieje na NAS-ie: {root}', file=sys.stderr)
        client.close()
        sys.exit(1)

    # Tryb: samo sprzątanie pustych katalogów (bez TMDB i przenoszenia)
    if args.cleanup_only:
        print(f'Biblioteka: {root}')
        print('Tryb: TYLKO SPRZĄTANIE pustych katalogów')
        _c, out, err = ssh_run(client, 'sh -s',
                               stdin_data='\n'.join(build_cleanup_lines(root, args.keep_vsmeta)) + '\n')
        if out:
            print(out, end='')
        if err.strip():
            print(err, file=sys.stderr, end='')
        removed = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO KATALOG:'))
        vsm = next((ln.split(':', 1)[1].strip()
                    for ln in out.split('\n') if ln.startswith('USUNIETO VSMETA:')), None)
        client.close()
        if vsm is not None:
            print(f'Usunięto plików .vsmeta: {vsm}')
        print(f'Usunięto pustych katalogów: {removed}')
        return

    # Tryb: usuwanie duplikatów tego samego odcinka
    if args.dedupe_only:
        print(f'Biblioteka: {root}')
        print('Tryb: USUWANIE DUPLIKATÓW (ten sam odcinek) '
              + ('— WYKONANIE' if apply else '— PODGLĄD (dodaj --apply aby skasować)'))
        _c, out, err = ssh_run(client, f"find {shlex.quote(root)} -type f -printf '%s\\t%p\\n'")
        if _c != 0 or not out.strip():
            _c, out, err = ssh_run(
                client, f"find {shlex.quote(root)} -type f -exec stat -c '%s\t%n' -- {{}} +")
        groups = {}
        for line in out.split('\n'):
            if '\t' not in line:
                continue
            size_s, path = line.split('\t', 1)
            base = path.rsplit('/', 1)[-1]
            if os.path.splitext(base)[1].lower() not in VIDEO_EXT:
                continue
            mk = find_marker(os.path.splitext(base)[0])
            if not mk:
                continue
            _s, _e2, season, episode = mk
            if season is None:
                season = 1
            try:
                size = int(size_s)
            except ValueError:
                size = 0
            d = path.rsplit('/', 1)[0]
            groups.setdefault((d, season, episode), []).append((size, path))

        losers = []
        for (d, season, episode), items in sorted(groups.items()):
            if len(items) < 2:
                continue
            items.sort(key=lambda x: (-x[0], x[1]))  # największy pierwszy, potem nazwa
            keep = items[0]
            print(f'S{season:02d}E{episode:02d} w {d[len(root):].lstrip("/")}:')
            print(f'  ZOSTAJE:  {keep[1].rsplit("/", 1)[-1]}  ({keep[0]} B)')
            for sz, p in items[1:]:
                print(f'  DUPLIKAT: {p.rsplit("/", 1)[-1]}  ({sz} B)')
                losers.append(p)

        if not losers:
            print('Nie znaleziono duplikatów.')
            client.close()
            return

        if apply:
            script = '\n'.join(
                f'rm -f -- {shlex.quote(p)} && printf "USUNIETO: %s\\n" {shlex.quote(p)}'
                for p in losers) + '\n'
            _c, o, e = ssh_run(client, 'sh -s', stdin_data=script)
            if o:
                print('---')
                print(o, end='')
            if e.strip():
                print(e, file=sys.stderr, end='')
            n = sum(1 for ln in o.split('\n') if ln.startswith('USUNIETO:'))
            client.close()
            print(f'\nUsunięto duplikatów: {n}')
        else:
            client.close()
            print(f'\nDuplikatów do usunięcia: {len(losers)}  (dodaj --apply aby skasować)')
        return

    code, out, err = ssh_run(client, f'find {shlex.quote(root)} -type f')
    if code != 0:
        print(f'Nie udało się wylistować plików: {err.strip()}', file=sys.stderr)
        client.close()
        sys.exit(1)

    all_files = [p for p in out.split('\n') if p]

    # Ile plików wideo jest w każdym katalogu (dla trybu --single-as-e01)
    dir_video_count = {}
    video_files = []
    for full in all_files:
        base = full.rsplit('/', 1)[-1]
        if os.path.splitext(base)[1].lower() in VIDEO_EXT:
            video_files.append(full)
            parent = full.rsplit('/', 1)[0]
            dir_video_count[parent] = dir_video_count.get(parent, 0) + 1

    groups = {}          # key -> {query, tmdbid, hint_year, files:[(full,s,e,release,ext)]}
    skipped_files = []

    for full in video_files:
        base = full.rsplit('/', 1)[-1]
        parent = full.rsplit('/', 1)[0]

        rel = full[len(root):].lstrip('/')
        parts = rel.split('/')
        ancestors = parts[:-1]

        season_hint = season_from_path(ancestors)
        parsed = parse_file(base, season_hint)
        if not parsed:
            if args.single_as_e01 and dir_video_count.get(parent, 0) == 1:
                t, rel_tags, fext = parse_unnumbered(base)
                parsed = (1, 1, t, rel_tags, fext)
            else:
                skipped_files.append((full, 'nie rozpoznano odcinka'))
                continue
        season, episode, title_from_file, release, fext = parsed

        tmdbid_known = None
        for a in ancestors:
            m = RE_TMDBID.search(a)
            if m:
                tmdbid_known = int(m.group(1))
                break

        query = series_source_name(ancestors, title_from_file)
        hint_year = series_year_hint(ancestors, base)
        key = f'id:{tmdbid_known}' if tmdbid_known else f'q:{normalize(query)}'
        g = groups.setdefault(key, {'query': query, 'tmdbid': tmdbid_known,
                                    'hint_year': hint_year, 'files': []})
        if not g['hint_year'] and hint_year:
            g['hint_year'] = hint_year
        g['files'].append((full, season, episode, release, fext))

    cache = load_cache(cache_path)
    cache_loaded = len(cache)
    plan = []
    unresolved = []
    planned = {}          # dest -> src (do wykrywania kolizji)
    collisions = []

    for g in groups.values():
        res = resolve_series(tmdb, g['query'], g['tmdbid'], g['hint_year'], cache, overrides)
        if not res.get('ok'):
            unresolved.append((g['query'], g['hint_year'], res.get('reason', '?'),
                               res.get('candidates', []), len(g['files'])))
            continue
        name, year, tid = res['name'], res['year'], res['id']
        folder = f'{name} ({year}) [tmdbid-{tid}]' if year else f'{name} [tmdbid-{tid}]'
        for full, season, episode, release, fext in g['files']:
            new_base = f'{name} S{season:02d}E{episode:02d}'
            if release:
                new_base += f'.{release}'
            new_base += fext
            dest = f'{root}/{folder}/Season {season:02d}/{new_base}'
            if dest == full:
                continue
            if dest in planned:
                collisions.append((full, planned[dest], dest))
                continue
            planned[dest] = full
            plan.append((full, dest))

    # Zapisz cache dopasowań TMDB (nowe rozwiązane seriale nie będą odpytywane ponownie)
    if cache_path:
        save_cache(cache_path, cache)

    print(f'Biblioteka: {root}')
    print('Tryb: ' + ('WYKONANIE' if apply else 'PODGLĄD (nic nie zmieniam)'))
    if cache_path:
        ok_now = sum(1 for v in cache.values() if isinstance(v, dict) and v.get('ok'))
        print(f'Cache TMDB: {cache_path} (wczytano: {cache_loaded}, zapisano: {ok_now})')
    if log_path:
        print(f'Log: {log_path}')
    print(f'Plików wideo do zmiany: {len(plan)}')
    print('---')
    for src, dest in plan:
        print(src[len(root):].lstrip('/'))
        print('   ->  ' + dest[len(root):].lstrip('/'))

    moved = 0
    skipped_exist = 0
    removed_dirs = 0
    apply_output = ''
    if apply:
        lines = []
        for src, dest in plan:
            dest_dir = dest.rsplit('/', 1)[0]
            qsrc, qdst, qdir = shlex.quote(src), shlex.quote(dest), shlex.quote(dest_dir)
            lines.append(
                f'mkdir -p {qdir}; '
                f'if [ -e {qdst} ]; then printf "POMIJAM (istnieje): %s\\n" {qdst}; '
                f'else mv -- {qsrc} {qdst} && printf "OK: %s\\n" {qdst}; fi'
            )
        if not args.no_cleanup_empty:
            lines += build_cleanup_lines(root, args.keep_vsmeta)
        if lines:
            _c, out, err = ssh_run(client, 'sh -s', stdin_data='\n'.join(lines) + '\n')
            if out:
                print('---')
                print(out, end='')
            if err.strip():
                print(err, file=sys.stderr, end='')
            apply_output = out
            moved = sum(1 for ln in out.split('\n') if ln.startswith('OK:'))
            skipped_exist = sum(1 for ln in out.split('\n') if ln.startswith('POMIJAM'))
            removed_dirs = sum(1 for ln in out.split('\n') if ln.startswith('USUNIETO KATALOG:'))
            vsm = next((ln.split(':', 1)[1].strip()
                        for ln in out.split('\n') if ln.startswith('USUNIETO VSMETA:')), None)
            if vsm is not None and vsm != '0':
                print(f'Usunięto plików .vsmeta: {vsm}')
            if removed_dirs:
                print(f'Usunięto pustych katalogów: {removed_dirs}')

    client.close()

    write_log(log_path, root, apply, plan, moved, skipped_exist, apply_output,
              collisions, skipped_files, unresolved)

    print('\n==================== PODSUMOWANIE ====================')
    if apply:
        print(f'Przeniesiono/zmieniono: {moved}, pominięto (istniały): {skipped_exist}')
    else:
        print(f'Zaplanowano zmian: {len(plan)}  (podgląd — dodaj --apply aby wykonać)')

    if collisions:
        print(f'\nKolizje (dwa pliki -> ta sama nazwa docelowa): {len(collisions)}')
        for src, other, dest in collisions[:20]:
            print(f'  {src}')
            print(f'  oraz {other}')
            print(f'  -> {dest}')

    if skipped_files:
        print(f'\nPominięte pliki ({len(skipped_files)}):')
        for full, why in skipped_files[:40]:
            print(f'  [{why}] {full}')

    if unresolved:
        print(f'\nSeriale bez jednoznacznego dopasowania TMDB ({len(unresolved)}):')
        for query, hint, reason, cands, nfiles in unresolved:
            hint_s = f', rok z folderu: {hint}' if hint else ''
            print(f'  "{query}"  — {reason}{hint_s}  ({nfiles} plik(ów))')
            for c in cands:
                print(f'      kandydat: {c["name"]} ({c["year"]})  tmdbid-{c["id"]}')
        print('\n  Aby wymusić dopasowanie, dopisz do tmdb_overrides.txt, np.:')
        print('      Nazwa serialu = 308975')


if __name__ == '__main__':
    main()
