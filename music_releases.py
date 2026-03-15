#!/usr/bin/env python3
"""
Music Release Aggregator
Scrapes new releases from alterportal.net and coreradio.online
Sorts by fan anticipation (views + weighted comments) and engagement rate.

Usage:
    python music_releases.py
    python music_releases.py --max 30 --output report.html
    python music_releases.py --sites alterportal --pages 2
    python music_releases.py --json releases.json
"""

import requests
from bs4 import BeautifulSoup
import re
from dataclasses import dataclass, field
from typing import Optional, List
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import argparse
import sys
import json
import os

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
TIMEOUT = 15
MAX_WORKERS = 8
DEFAULT_MAX = 25

# Genres to exclude (case-insensitive substring match)
BLOCKED_GENRES = [
    "deathcore",
    "thrash metal",
    "death metal",
    "black metal",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class Release:
    title: str           # "Artist - Album (Year)"
    artist: str
    album: str
    year: str
    source: str          # 'alterportal' | 'coreradio'
    url: str
    genre: str = ""
    country: str = ""
    fmt: str = ""
    views: int = 0
    comments: int = 0
    date_str: str = ""
    cover_url: str = ""
    tags: List[str] = field(default_factory=list)
    release_type: str = "LP"   # "LP" | "EP" | "Single"
    spotify_fav: bool = False   # True if artist is in user's Spotify playlist

    @property
    def anticipation_score(self) -> int:
        """Views + heavily weighted comments (active discussion signals strong interest)."""
        return self.views + self.comments * 15

    @property
    def engagement_rate(self) -> float:
        """Comments per 100 views — higher means more active discussion relative to traffic."""
        if self.views == 0:
            return float(self.comments * 10)
        return round((self.comments / self.views) * 100, 2)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
_session = requests.Session()
_session.headers.update(HEADERS)


def fetch(url: str) -> Optional[BeautifulSoup]:
    try:
        r = _session.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        r.encoding = r.apparent_encoding
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"  [warn] fetch failed: {url} — {e}", file=sys.stderr)
        return None


def parse_int(text: str) -> int:
    """'1 791' → 1791"""
    cleaned = re.sub(r"[^\d]", "", text or "")
    return int(cleaned) if cleaned else 0


# ---------------------------------------------------------------------------
# alterportal.net scraper
# ---------------------------------------------------------------------------
def scrape_alterportal_page(url: str) -> List[Release]:
    soup = fetch(url)
    if not soup:
        return []

    releases = []
    for card in soup.select("article.short"):
        try:
            title_a = card.select_one(".short_title a")
            if not title_a:
                continue

            title_full = title_a.get_text(strip=True)
            release_url = title_a["href"]

            # Parse "Artist - Album (Year)"
            m = re.match(r"^(.+?)\s*[-–]\s*(.+?)\s*\((\d{4})\)\s*$", title_full)
            artist = m.group(1).strip() if m else title_full
            album  = m.group(2).strip() if m else ""
            year   = m.group(3)         if m else ""

            # Cover image
            img = card.select_one("img")
            cover_url = img.get("src", "") if img else ""

            # Tags/categories
            tags = [a.get_text(strip=True) for a in card.select(".short_cat a")]

            # Genre and format from release text block
            short_text = card.select_one(".short_text")
            raw_text = short_text.get_text("\n") if short_text else ""
            genre_m = re.search(r"[Сс]тиль\s*:+\s*(.+)", raw_text)
            fmt_m   = re.search(r"[Фф]ормат\s*:+\s*(.+)", raw_text)
            genre = genre_m.group(1).strip() if genre_m else ""
            fmt   = fmt_m.group(1).strip()   if fmt_m   else ""

            # Views: <b> inside .noselect
            views_b = card.select_one(".noselect b")
            views = parse_int(views_b.get_text()) if views_b else 0

            # Comments: <b> inside the comment-count anchor
            comm_b = card.select_one(".story_dalee.sdmt10 b")
            comments = parse_int(comm_b.get_text()) if comm_b else 0

            # Date from story_bottom
            story_bottom = card.select_one(".story_bottom")
            date_str = ""
            if story_bottom:
                bt = story_bottom.get_text(" ", strip=True)
                dm = re.search(
                    r"(\d+\s+\w+\s+\d{4}|[Сс]егодня[,\s]*\d{2}:\d{2}|[Вв]чера[,\s]*\d{2}:\d{2})",
                    bt,
                )
                date_str = dm.group(1) if dm else ""

            releases.append(Release(
                title=title_full,
                artist=artist,
                album=album,
                year=year,
                source="alterportal",
                url=release_url,
                genre=genre,
                fmt=fmt,
                views=views,
                comments=comments,
                date_str=date_str,
                cover_url=cover_url,
                tags=tags,
            ))
        except Exception as e:
            print(f"  [warn] alterportal card parse error: {e}", file=sys.stderr)

    return releases


def scrape_alterportal(max_releases: int = DEFAULT_MAX, pages: int = 1) -> List[Release]:
    print("[alterportal.net] Fetching releases...")
    releases: List[Release] = []
    seen_urls: set = set()

    page_urls = ["https://alterportal.net/"]
    for p in range(2, pages + 1):
        page_urls.append(f"https://alterportal.net/page/{p}/")

    for purl in page_urls:
        batch = scrape_alterportal_page(purl)
        for r in batch:
            if r.url not in seen_urls:
                seen_urls.add(r.url)
                releases.append(r)
        if len(releases) >= max_releases:
            break

    releases = releases[:max_releases]
    print(f"[alterportal.net] Got {len(releases)} releases")
    return releases


# ---------------------------------------------------------------------------
# coreradio.online scraper
# ---------------------------------------------------------------------------
def parse_coreradio_detail(url: str) -> Optional[Release]:
    soup = fetch(url)
    if not soup:
        return None
    try:
        # Title from <title>
        raw_title = soup.title.get_text(strip=True)
        raw_title = re.sub(r"\s*»\s*CORE RADIO.*$", "", raw_title).strip()
        m = re.match(r"^(.+?)\s*[-–]\s*(.+?)\s*\((\d{4})\)\s*$", raw_title)
        artist = m.group(1).strip() if m else raw_title
        album  = m.group(2).strip() if m else ""
        year   = m.group(3)         if m else ""

        # Genre, country, quality from info block
        info = soup.select_one(".full-news-info")
        info_text = info.get_text("\n") if info else ""
        genre_m   = re.search(r"Genre:\s*(.+)",   info_text)
        country_m = re.search(r"Country:\s*(.+)", info_text)
        quality_m = re.search(r"Quality:\s*(.+)", info_text)
        genre   = genre_m.group(1).strip()   if genre_m   else ""
        country = country_m.group(1).strip() if country_m else ""
        fmt     = quality_m.group(1).strip() if quality_m else ""

        # Cover from og:image or first image in left column
        og_img = soup.find("meta", property="og:image")
        cover_url = og_img["content"] if og_img and og_img.get("content") else ""
        if not cover_url:
            img = soup.select_one(".full-news-left img")
            cover_url = img.get("src", "") if img else ""

        # Views from .fullo-news-line (the stats line below the release)
        fullo = soup.select_one(".fullo-news-line")
        views = 0
        date_str = ""
        if fullo:
            fullo_text = fullo.get_text(" ", strip=True)
            nums = re.findall(r"[\d][\d\s]*", fullo_text)
            if nums:
                views = parse_int(nums[0])
            date_m = re.search(
                r"(January|February|March|April|May|June|July|August"
                r"|September|October|November|December)\s+\d+,\s+\d{4}",
                fullo_text,
            )
            date_str = date_m.group(0) if date_m else ""

        # Comment count — count comment wrapper elements
        comments = 0
        for sel in (".fi", ".comment-item", "[id^='comment-id-']"):
            n = len(soup.select(sel))
            if n > comments:
                comments = n

        return Release(
            title=raw_title,
            artist=artist,
            album=album,
            year=year,
            source="coreradio",
            url=url,
            genre=genre,
            country=country,
            fmt=fmt,
            views=views,
            comments=comments,
            date_str=date_str,
            cover_url=cover_url,
        )
    except Exception as e:
        print(f"  [warn] coreradio detail parse error {url}: {e}", file=sys.stderr)
        return None


def scrape_coreradio(max_releases: int = DEFAULT_MAX) -> List[Release]:
    print("[coreradio.online] Fetching main page...")
    soup = fetch("https://coreradio.online/")
    if not soup:
        return []

    # Collect unique release URLs — pattern /{genre}/{id}-{slug}-{year}
    seen: set = set()
    release_urls: List[str] = []
    release_re = re.compile(
        r"https://coreradio\.online/[a-z0-9-]+/\d+-[a-z0-9-]+-\d{4}$"
    )
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if release_re.match(href) and href not in seen:
            seen.add(href)
            release_urls.append(href)

    release_urls = release_urls[:max_releases]
    print(
        f"[coreradio.online] Found {len(release_urls)} release URLs, "
        f"fetching detail pages..."
    )

    releases: List[Release] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(parse_coreradio_detail, url): url
            for url in release_urls
        }
        total = len(futures)
        done = 0
        for future in as_completed(futures):
            result = future.result()
            done += 1
            print(f"  [{done}/{total}] fetched", end="\r", flush=True)
            if result:
                releases.append(result)
    print()

    print(f"[coreradio.online] Got {len(releases)} releases")
    return releases


# ---------------------------------------------------------------------------
# Filtering and deduplication
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    """Normalise a string for fuzzy matching: lowercase, strip punctuation/spaces."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def is_blocked(r: Release) -> bool:
    genre_lower = r.genre.lower()
    return any(g in genre_lower for g in BLOCKED_GENRES)


def detect_release_type(r: Release) -> str:
    """Detect LP / EP / Single from album name, title, URL, tags, genre, fmt."""
    combined = " ".join([
        r.album.lower(), r.title.lower(),
        " ".join(r.tags).lower(), r.genre.lower(), r.fmt.lower(),
    ])
    url_lower = r.url.lower()

    single_re = re.compile(r"\bsingles?\b|сингл|\(single\)|\[single\]")
    if single_re.search(combined) or "/single" in url_lower:
        return "Single"

    ep_re = re.compile(r"\bep\b|\(ep\)|\[ep\]|мини.альбом|\bmaxi\b")
    if ep_re.search(combined) or "/ep" in url_lower:
        return "EP"

    return "LP"


def filter_and_deduplicate(releases: List[Release]) -> List[Release]:
    # 1. Genre filter
    filtered = [r for r in releases if not is_blocked(r)]
    removed = len(releases) - len(filtered)
    if removed:
        print(f"[filter] Removed {removed} releases matching blocked genres")

    # 2. Deduplicate: same artist + album across sites → keep entry with more
    #    views; mark source as "both" and sum comment counts.
    seen: dict = {}  # norm_key -> index in result list
    result: List[Release] = []
    for r in filtered:
        key = _norm(r.artist) + "|" + _norm(r.album)
        if not key or key == "|":
            result.append(r)
            continue
        if key in seen:
            existing = result[seen[key]]
            # Merge: pick higher view count, sum comments, mark source "both"
            merged_views    = max(existing.views, r.views)
            merged_comments = existing.comments + r.comments
            merged_cover    = existing.cover_url or r.cover_url
            merged_genre    = existing.genre or r.genre
            merged_country  = existing.country or r.country
            merged_fmt      = existing.fmt or r.fmt
            merged_date     = existing.date_str or r.date_str
            merged_tags     = list(dict.fromkeys(existing.tags + r.tags))
            result[seen[key]] = Release(
                title=existing.title,
                artist=existing.artist,
                album=existing.album,
                year=existing.year,
                source="both",
                url=existing.url,
                genre=merged_genre,
                country=merged_country,
                fmt=merged_fmt,
                views=merged_views,
                comments=merged_comments,
                date_str=merged_date,
                cover_url=merged_cover,
                tags=merged_tags,
            )
        else:
            seen[key] = len(result)
            result.append(r)

    dupes = len(filtered) - len(result)
    if dupes:
        print(f"[dedup]  Merged {dupes} duplicate releases found on both sites")

    # 3. Detect release type for every remaining release
    for r in result:
        r.release_type = detect_release_type(r)

    return result



# ---------------------------------------------------------------------------
# Favourite artists (loaded from a text file, one artist per line)
# ---------------------------------------------------------------------------
def load_favourite_artists(path: str = "") -> set:
    """Return a set of normalised artist names from a text file."""
    if not path:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "favorite_artists.txt")
    try:
        with open(path, encoding="utf-8") as f:
            artists = set()
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    artists.add(_norm(line))
            print(f"[favs] Loaded {len(artists)} favourite artists from {path}")
            return artists
    except FileNotFoundError:
        return set()


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------
HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>New Music Releases — {date}</title>
<style>
  :root {{
    --bg:#0e0e0e; --card:#181818; --border:#2a2a2a; --text:#e0e0e0;
    --sub:#777; --accent:#4a9eff; --alt:#ff6b35; --core:#3db87a;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; padding:28px 20px; max-width:1600px; margin:0 auto; }}
  h1 {{ font-size:1.6rem; font-weight:700; margin-bottom:4px; }}
  .subtitle {{ color:var(--sub); font-size:0.82rem; margin-bottom:22px; }}
  .tabs {{ display:flex; gap:10px; margin-bottom:22px; flex-wrap:wrap; }}
  .tab {{
    padding:7px 16px; border-radius:6px; background:var(--card);
    border:1px solid var(--border); cursor:pointer; font-size:0.83rem;
    color:var(--text); transition:background .15s,border-color .15s;
  }}
  .tab:hover {{ background:#222; }}
  .tab.active {{ background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }}
  .grid {{
    display:grid;
    grid-template-columns:repeat(auto-fill, minmax(260px, 1fr));
    gap:14px;
  }}
  .card {{
    background:var(--card); border:1px solid var(--border); border-radius:10px;
    overflow:hidden; display:flex; flex-direction:column;
    transition:border-color .18s, transform .18s;
  }}
  .card:hover {{ border-color:#555; transform:translateY(-2px); }}
  .card-cover {{ width:100%; aspect-ratio:1/1; object-fit:cover; background:#222; display:block; }}
  .card-cover-ph {{ width:100%; aspect-ratio:1/1; background:#1c1c1c; display:flex; align-items:center; justify-content:center; font-size:2.5rem; }}
  .card-body {{ padding:12px; flex:1; display:flex; flex-direction:column; gap:6px; }}
  .card-top {{ display:flex; justify-content:space-between; align-items:center; }}
  .rank {{ font-size:0.7rem; color:var(--sub); font-weight:700; letter-spacing:.5px; }}
  .badge {{
    display:inline-block; padding:2px 7px; border-radius:4px;
    font-size:0.62rem; font-weight:700; letter-spacing:.5px; text-transform:uppercase;
  }}
  .badge-alt {{ background:rgba(255,107,53,.18); color:var(--alt); }}
  .badge-core {{ background:rgba(61,184,122,.18); color:var(--core); }}
  .badge-both {{ background:rgba(155,92,255,.18); color:#b07aff; }}
  .card-artist {{ font-size:0.95rem; font-weight:600; line-height:1.3; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .card-album {{ font-size:0.82rem; color:var(--sub); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .card-genre {{ font-size:0.72rem; color:var(--accent); margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .tags {{ display:flex; flex-wrap:wrap; gap:3px; margin-top:2px; }}
  .tag {{ background:#222; border:1px solid #333; border-radius:3px; padding:1px 5px; font-size:0.62rem; color:var(--sub); }}
  .score-bar {{ height:3px; background:var(--border); border-radius:2px; margin-top:4px; }}
  .score-fill {{ height:100%; border-radius:2px; }}
  .fill-ant {{ background:linear-gradient(90deg,#4a9eff,#9b5cff); }}
  .fill-eng {{ background:linear-gradient(90deg,#3db87a,#a8e063); }}
  .stats {{ display:flex; gap:10px; margin-top:auto; padding-top:8px; border-top:1px solid var(--border); font-size:0.73rem; color:var(--sub); flex-wrap:wrap; }}
  .stat {{ display:flex; align-items:center; gap:3px; }}
  .stat b {{ color:var(--text); }}
  .section {{ display:none; }}
  .section.active {{ display:block; }}
  a {{ color:inherit; text-decoration:none; }}
  .no-data {{ color:var(--sub); font-size:0.9rem; padding:20px 0; }}
  .cover-wrap {{ position:relative; }}
  .type-pill {{
    position:absolute; top:6px; left:6px;
    padding:2px 8px; border-radius:4px;
    font-size:0.6rem; font-weight:800; letter-spacing:.6px; text-transform:uppercase;
    backdrop-filter:blur(6px); pointer-events:none;
  }}
  .type-lp     {{ background:rgba(74,158,255,.82); color:#fff; }}
  .type-ep     {{ background:rgba(255,167,53,.85); color:#fff; }}
  .type-single {{ background:rgba(61,184,122,.85); color:#fff; }}
  .filter-chips {{ display:flex; gap:8px; margin:-8px 0 20px; flex-wrap:wrap; }}
  .chip {{
    padding:4px 14px; border-radius:20px; background:var(--card);
    border:1px solid var(--border); cursor:pointer; font-size:0.76rem;
    color:var(--sub); transition:all .15s;
  }}
  .chip:hover {{ border-color:#555; color:var(--text); }}
  .chip.active {{ background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }}
  .card.hidden {{ display:none; }}
  .card.fav {{ border-color:#f4c430; box-shadow:0 0 0 1px #f4c43044; }}
  .badge-fav {{ background:rgba(244,196,48,.18); color:#f4c430; }}
  .fav-star {{ font-size:0.75rem; }}
</style>
</head>
<body>

<h1>🎵 New Music Releases</h1>
<div class="subtitle">
  Aggregated from <strong>alterportal.net</strong> &amp; <strong>coreradio.online</strong>
  &nbsp;·&nbsp; {date}
  &nbsp;·&nbsp; {total} releases
</div>

<div class="tabs">
  <button class="tab active" onclick="show('anticipation',this)">🔥 Fan Anticipation</button>
  <button class="tab" onclick="show('engagement',this)">💬 Engagement Rate</button>
</div>
<div class="filter-chips">
  <button class="chip active" onclick="filterType('all',this)">All</button>
  <button class="chip" onclick="filterType('LP',this)">LPs</button>
  <button class="chip" onclick="filterType('EP',this)">EPs</button>
  <button class="chip" onclick="filterType('Single',this)">Singles</button>
  {fav_chip}
</div>

{sections}

<script>
let _typeFilter = 'all';

function applyFilter() {{
  document.querySelectorAll('.card').forEach(card => {{
    const typeOk = _typeFilter === 'all' || _typeFilter === 'fav'
                   ? true
                   : card.dataset.type === _typeFilter;
    const favOk  = _typeFilter !== 'fav' || card.dataset.fav === '1';
    card.parentElement.style.display = (typeOk && favOk) ? '' : 'none';
  }});
}}

function show(id, btn) {{
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('sec-' + id).classList.add('active');
  btn.classList.add('active');
  applyFilter();
}}

function filterType(type, btn) {{
  _typeFilter = type;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  applyFilter();
}}
</script>
</body>
</html>
"""


def render_card(r: Release, rank: int, max_score: float, fill_class: str) -> str:
    if r.source == "alterportal":
        badge_cls, badge_label = "badge-alt",  "Alterportal"
    elif r.source == "coreradio":
        badge_cls, badge_label = "badge-core", "Core Radio"
    else:
        badge_cls, badge_label = "badge-both", "Both Sites"

    fav_badge = '<span class="badge badge-fav fav-star">⭐ In Playlist</span>' if r.spotify_fav else ""
    fav_cls   = " fav" if r.spotify_fav else ""
    fav_attr  = ' data-fav="1"' if r.spotify_fav else ""

    score    = r.anticipation_score if fill_class == "fill-ant" else r.engagement_rate
    fill_pct = round(min((score / max_score * 100), 100)) if max_score > 0 else 0

    tags_html = "".join(
        f'<span class="tag">{t}</span>' for t in r.tags[:4]
    ) if r.tags else ""

    type_pill = (
        f'<span class="type-pill type-{r.release_type.lower()}">'
        f'{r.release_type}</span>'
    )
    if r.cover_url:
        img_html = (
            f'<img class="card-cover" src="{r.cover_url}" '
            f'loading="lazy" alt="" onerror="this.style.display=\'none\'">'
        )
    else:
        img_html = '<div class="card-cover-ph">🎵</div>'
    cover_html = f'<div class="cover-wrap">{img_html}{type_pill}</div>'

    country_bit = f" · {r.country}" if r.country else ""
    date_bit    = f'<span class="stat">📅 <b>{r.date_str}</b></span>' if r.date_str else ""

    return (
        f'<a href="{r.url}" target="_blank" rel="noopener">'
        f'<div class="card{fav_cls}" data-type="{r.release_type}"{fav_attr}>'
        f"{cover_html}"
        f'<div class="card-body">'
        f'<div class="card-top">'
        f'<span class="rank">#{rank}</span>'
        f'<span class="badge {badge_cls}">{badge_label}</span>'
        f"</div>"
        f"{fav_badge}"
        f'<div class="card-artist">{r.artist or r.title}</div>'
        f'<div class="card-album">{r.album}</div>'
        f'<div class="card-genre">{(r.genre or "—")[:70]}{country_bit}</div>'
        f'{"<div class=tags>" + tags_html + "</div>" if tags_html else ""}'
        f'<div class="score-bar"><div class="score-fill {fill_class}" style="width:{fill_pct}%"></div></div>'
        f'<div class="stats">'
        f'<span class="stat">👁 <b>{r.views:,}</b></span>'
        f'<span class="stat">💬 <b>{r.comments}</b></span>'
        f'<span class="stat">📊 <b>{r.engagement_rate:.1f}%</b></span>'
        f"{date_bit}"
        f"</div>"
        f"</div>"
        f"</div>"
        f"</a>"
    )


def render_section(
    releases: List[Release],
    sort_key,
    fill_class: str,
    section_id: str,
    active: bool = False,
) -> str:
    active_cls = " active" if active else ""
    if not releases:
        return (
            f'<div class="section{active_cls}" id="sec-{section_id}">'
            f'<p class="no-data">No releases found.</p></div>'
        )
    sorted_r = sorted(releases, key=sort_key, reverse=True)
    max_score = sort_key(sorted_r[0]) or 1
    cards = "".join(
        render_card(r, i + 1, max_score, fill_class)
        for i, r in enumerate(sorted_r)
    )
    return (
        f'<div class="section{active_cls}" id="sec-{section_id}">'
        f'<div class="grid">{cards}</div>'
        f"</div>"
    )


def generate_html(all_releases: List[Release], output_path: str) -> None:
    date_str = datetime.now().strftime("%B %d, %Y")

    sections = "".join([
        render_section(all_releases, lambda r: r.anticipation_score, "fill-ant", "anticipation", active=True),
        render_section(all_releases, lambda r: r.engagement_rate,    "fill-eng", "engagement"),
    ])

    has_favs = any(r.spotify_fav for r in all_releases)
    fav_chip = (
        '<button class="chip" onclick="filterType(\'fav\',this)">⭐ In My Playlist</button>'
        if has_favs else ""
    )

    html = HTML_TEMPLATE.format(
        date=date_str,
        total=len(all_releases),
        sections=sections,
        fav_chip=fav_chip,
    )
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nDone. HTML report saved -> {output_path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    # Ensure UTF-8 output on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Scrape new music releases and generate a sorted HTML report."
    )
    parser.add_argument(
        "--max", type=int, default=DEFAULT_MAX,
        help=f"Max releases per site (default: {DEFAULT_MAX})",
    )
    parser.add_argument(
        "--pages", type=int, default=1,
        help="Number of alterportal pages to scrape (default: 1)",
    )
    parser.add_argument(
        "--output", default="releases.html",
        help="Output HTML file (default: releases.html)",
    )
    parser.add_argument(
        "--json", default="",
        metavar="FILE",
        help="Also save raw data as JSON",
    )
    parser.add_argument(
        "--sites", default="both",
        choices=["both", "alterportal", "coreradio"],
        help="Which sites to scrape (default: both)",
    )
    parser.add_argument(
        "--fav-artists", default="",
        metavar="FILE",
        help="Path to text file with favourite artist names (one per line)",
    )
    args = parser.parse_args()

    all_releases: List[Release] = []

    if args.sites in ("both", "alterportal"):
        all_releases.extend(scrape_alterportal(args.max, args.pages))

    if args.sites in ("both", "coreradio"):
        all_releases.extend(scrape_coreradio(args.max))

    if not all_releases:
        print("No releases found. Exiting.")
        sys.exit(1)

    all_releases = filter_and_deduplicate(all_releases)

    if not all_releases:
        print("No releases remaining after filtering. Exiting.")
        sys.exit(1)

    # Favourite artist matching
    fav_artists = load_favourite_artists(args.fav_artists)
    if fav_artists:
        for r in all_releases:
            if _norm(r.artist) in fav_artists:
                r.spotify_fav = True
        favs = sum(1 for r in all_releases if r.spotify_fav)
        print(f"[favs] Marked {favs} releases as favourites")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {k: v for k, v in r.__dict__.items()}
                    | {
                        "anticipation_score": r.anticipation_score,
                        "engagement_rate": r.engagement_rate,
                    }
                    for r in all_releases
                ],
                f,
                ensure_ascii=False,
                indent=2,
            )
        print(f"JSON saved → {args.json}")

    generate_html(all_releases, args.output)

    # Terminal summary
    top = sorted(all_releases, key=lambda r: r.anticipation_score, reverse=True)[:10]
    print("\nTop 10 by Fan Anticipation:")
    print(f"  {'#':<3} {'Title':<45} {'Source':<12} {'Views':>7} {'Cmts':>5} {'Score':>7}")
    print("  " + "-" * 82)
    for i, r in enumerate(top, 1):
        title = r.title[:43]
        print(
            f"  {i:<3} {title:<45} {r.source:<12} "
            f"{r.views:>7,} {r.comments:>5} {r.anticipation_score:>7,}"
        )


if __name__ == "__main__":
    main()
