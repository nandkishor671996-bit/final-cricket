#!/usr/bin/env python3
"""
Cricket Live Overlay Server — FINAL
=====================================
Data pipeline:
  Step 1: Scrape live match list from Cricbuzz RSS + TOI/CREX HTML
  Step 2: Scrape detailed match page (TOI match-center) for rich data
           (batsmen, bowlers, balls, CRR, RRR, partnership, etc.)
  Step 3: Use Gemini 2.0 Flash to structure raw scraped text → clean JSON
  Step 4: Write data.json every POLL_INTERVAL seconds
  Step 5: Serve livematch.html + data.json via HTTP

Smarts:
  - Gemini is called ONLY when scraped content changes (saves free quota)
  - Falls back to regex parse if Gemini unavailable
  - Falls back to RSS if HTML scraping blocked

Deploy:
  Local  → python3 server.py
  Railway→ set GEMINI_API_KEY env var, push to GitHub, done
"""

import os, json, time, re, threading, logging, hashlib
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urljoin

import requests
import feedparser
from bs4 import BeautifulSoup
from google import genai
from google.genai import types

# ─── Config ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY  = os.environ.get("GEMINI_API_KEY", "")
PORT            = int(os.environ.get("PORT", 8080))
POLL_INTERVAL   = 90    # seconds — safe within Gemini free tier (1000 req/day)
BASE_DIR        = Path(__file__).parent
DATA_FILE       = BASE_DIR / "data.json"
HTML_FILE       = BASE_DIR / "livematch.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("cricket")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
}

# ─── Fallback data.json skeleton ───────────────────────────────────────────────
FALLBACK = {
    "team1": {"name": "India",  "score": "---", "overs": "", "flag_img": ""},
    "team2": {"name": "TBD",    "score": "---", "overs": "", "flag_img": ""},
    "match_status": "Fetching live data...",
    "crr": "---", "rrr": "---", "target": "---", "partnership": "0(0)",
    "need": "", "last_wicket": "---",
    "current_over": 0, "current_ball": "", "last_over_balls": [],
    "yet_to_bat": "---",
    "batsman1": {"name":"BATSMAN 1","runs":0,"balls":0,"fours":0,"sixes":0,"sr":"0.00","on_strike":True, "photo":""},
    "batsman2": {"name":"BATSMAN 2","runs":0,"balls":0,"fours":0,"sixes":0,"sr":"0.00","on_strike":False,"photo":""},
    "bowler":   {"name":"BOWLER",   "wickets":0,"runs":0,"overs":"0","maidens":0,"economy":"0.00","photo":""},
    "venue": {"name": ""},
    "last_updated": "",
}

# ─── Gemini prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are a live cricket data extractor.
Given raw scraped cricket match text, return ONLY a valid JSON object.
No markdown, no explanation, no code fences — pure JSON only.

JSON schema (fill every field):
{
  "team1": {"name": str, "score": str, "overs": str, "flag_img": ""},
  "team2": {"name": str, "score": str, "overs": str, "flag_img": ""},
  "match_status": str,
  "crr": str,
  "rrr": str,
  "target": str,
  "partnership": str,
  "need": str,
  "last_wicket": str,
  "current_over": int,
  "current_ball": str,
  "last_over_balls": [str],
  "yet_to_bat": str,
  "batsman1": {"name":str,"runs":int,"balls":int,"fours":int,"sixes":int,"sr":str,"on_strike":bool,"photo":""},
  "batsman2": {"name":str,"runs":int,"balls":int,"fours":int,"sixes":int,"sr":str,"on_strike":bool,"photo":""},
  "bowler":   {"name":str,"wickets":int,"runs":int,"overs":str,"maidens":int,"economy":str,"photo":""},
  "venue": {"name": str},
  "last_updated": str
}

Rules:
- team1 = batting team, team2 = bowling/fielding team
- score format: "182/4" — put overs separately e.g. "19.2"
- match_status: short phrase like "LIVE", "INNINGS BREAK", "IND need 43 off 24 balls", "Match Over"
- current_ball: last ball — one of "0","1","2","3","4","6","W","WD","NB"
- last_over_balls: up to 6 ball outcomes e.g. ["1","0","4","W","2","6"]
- last_updated: current UTC time as "HH:MM:SS UTC"
- crr/rrr: decimal string e.g. "8.45" or "---"
- Defaults: 0 for ints, "---" for unknown strings, "" for optional strings
- Pick the most interesting LIVE match; if no live match, most recent completed
"""

# ══════════════════════════════════════════════════════════════════════════════
#  SCRAPING LAYER
# ══════════════════════════════════════════════════════════════════════════════

def _get(url, timeout=6, extra_headers=None):
    h = {**HEADERS, **(extra_headers or {})}
    r = requests.get(url, headers=h, timeout=timeout)
    r.raise_for_status()
    return r.text


# ── Source 1: Cricbuzz RSS (basic, very reliable) ─────────────────────────────
def scrape_cricbuzz_rss() -> str:
    url = "https://www.cricbuzz.com/live-cricket-scores/rss"
    try:
        r = requests.get(url, headers=HEADERS, timeout=6)
        feed = feedparser.parse(r.text)
        lines = []
        for e in feed.entries[:8]:
            lines.append(f"TITLE: {e.get('title','')}")
            lines.append(f"DESC: {e.get('summary', e.get('description',''))}")
        result = "\n".join(lines)
        if result.strip():
            log.info(f"Cricbuzz RSS: {len(feed.entries)} items")
            return result
    except Exception as ex:
        log.warning(f"Cricbuzz RSS failed: {ex}")
    return ""


# ── Source 2: Cricbuzz HTML live scores (rich, has scores inline) ─────────────
def scrape_cricbuzz_html() -> str:
    try:
        html = _get("https://www.cricbuzz.com/cricket-match/live-scores",
                    extra_headers={"Host": "www.cricbuzz.com"})
        soup = BeautifulSoup(html, "html.parser")
        blocks = []
        for card in soup.select(".cb-lv-scrs-col, .cb-mtch-lst")[:6]:
            text = card.get_text(" ", strip=True)
            if text and len(text) > 10:
                blocks.append(text)
        result = "\n---\n".join(blocks)
        if result.strip():
            log.info(f"Cricbuzz HTML: {len(blocks)} match cards")
            return result
    except Exception as ex:
        log.warning(f"Cricbuzz HTML failed: {ex}")
    return ""


# ── Source 3: CREX.live HTML (often unblocked) ───────────────────────────────
def scrape_crex() -> str:
    try:
        html = _get("https://crex.live/fixtures/live",
                    extra_headers={"Host": "crex.live"})
        soup = BeautifulSoup(html, "html.parser")
        blocks = []
        for card in soup.select(".match-card-container")[:6]:
            text = card.get_text(" ", strip=True)
            if text:
                blocks.append(text)
        result = "\n---\n".join(blocks)
        if result.strip():
            log.info(f"CREX: {len(blocks)} match cards")
            return result
    except Exception as ex:
        log.warning(f"CREX failed: {ex}")
    return ""


# ── Source 4: Times of India live score page (richest detail) ─────────────────
def scrape_toi_list() -> list[str]:
    """Returns list of live match URLs from TOI cricket page."""
    urls = []
    try:
        html = _get("https://timesofindia.indiatimes.com/sports/cricket/live-score",
                    extra_headers={"Host": "timesofindia.indiatimes.com", "Referer": "https://timesofindia.indiatimes.com/"})
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href*='match-center'], a[href*='live-score-update']"):
            href = a.get("href", "")
            if href:
                full = urljoin("https://timesofindia.indiatimes.com", href)
                if full not in urls:
                    urls.append(full)
        log.info(f"TOI match URLs found: {len(urls)}")
    except Exception as ex:
        log.warning(f"TOI list failed: {ex}")
    return urls[:3]


def scrape_toi_match(url: str) -> str:
    """Scrape detailed match data from TOI match-center page."""
    try:
        html = _get(url, timeout=8, extra_headers={
            "Host": "timesofindia.indiatimes.com",
            "Referer": "https://timesofindia.indiatimes.com/"
        })
        soup = BeautifulSoup(html, "html.parser")

        parts = []

        # Match title
        title = soup.select_one(".yVIFA, .match-name, h1")
        if title:
            parts.append(f"MATCH: {title.get_text(strip=True)}")

        # Status
        status = soup.select_one(".rfooB, .match-status, .live-status")
        if status:
            parts.append(f"STATUS: {status.get_text(strip=True)}")

        # Teams + scores
        for i, team in enumerate(soup.select(".XmDIn")[:2]):
            name = team.select_one(".K_p_P")
            score = team.select_one(".t_S_c")
            overs = team.select_one(".o_V_r")
            parts.append(
                f"TEAM{i+1}: {name and name.get_text(strip=True) or 'TBD'} "
                f"SCORE: {score and score.get_text(strip=True) or '---'} "
                f"OVERS: {overs and overs.get_text(strip=True) or ''}"
            )

        # Batsmen
        for row in soup.select(".b_t_s_m_n_r_w")[:2]:
            nm = row.select_one(".b_t_s_m_n_n_m")
            rn = row.select_one(".b_t_s_m_n_r_n")
            bl = row.select_one(".b_t_s_m_n_b_l")
            f4 = row.select_one(".b_t_s_m_n_f_r")
            s6 = row.select_one(".b_t_s_m_n_s_x")
            sr = row.select_one(".b_t_s_m_n_s_r")
            striker = bool(row.select(".b_t_s_m_n_s_t, .striker-icon, .active-bat"))
            if nm:
                parts.append(
                    f"BATSMAN: {nm.get_text(strip=True)} "
                    f"R:{rn and rn.get_text(strip=True) or 0} "
                    f"B:{bl and bl.get_text(strip=True) or 0} "
                    f"4s:{f4 and f4.get_text(strip=True) or 0} "
                    f"6s:{s6 and s6.get_text(strip=True) or 0} "
                    f"SR:{sr and sr.get_text(strip=True) or '0.00'} "
                    f"STRIKER:{striker}"
                )

        # Bowlers
        for row in soup.select(".b_w_l_r_r_w")[:2]:
            nm = row.select_one(".b_w_l_r_n_m")
            ov = row.select_one(".b_w_l_r_o_v")
            md = row.select_one(".b_w_l_r_m_d")
            rn = row.select_one(".b_w_l_r_r_n")
            wk = row.select_one(".b_w_l_r_w_k")
            ec = row.select_one(".b_w_l_r_e_c")
            if nm:
                parts.append(
                    f"BOWLER: {nm.get_text(strip=True)} "
                    f"O:{ov and ov.get_text(strip=True) or '0'} "
                    f"M:{md and md.get_text(strip=True) or 0} "
                    f"R:{rn and rn.get_text(strip=True) or 0} "
                    f"W:{wk and wk.get_text(strip=True) or 0} "
                    f"ECO:{ec and ec.get_text(strip=True) or '0.00'}"
                )

        # Recent balls
        balls = [b.get_text(strip=True) for b in soup.select(".r_c_n_t_b_l_s .b_l_l, .recent-balls .ball")]
        if balls:
            parts.append(f"RECENT_BALLS: {' '.join(balls[-6:])}")

        # This over
        this_over = [b.get_text(strip=True) for b in soup.select(".this-over .ball, .current-over .ball")]
        if this_over:
            parts.append(f"THIS_OVER: {' '.join(this_over[-6:])}")

        # Stats: CRR, RRR, partnership, last wicket
        for sel, label in [
            (".crr, .current-run-rate", "CRR"),
            (".rrr, .required-run-rate", "RRR"),
            (".p_r_t_n_r_s_h_p, .partnership", "PARTNERSHIP"),
            (".l_s_t_w_k_t, .last-wicket", "LAST_WICKET"),
        ]:
            el = soup.select_one(sel)
            if el:
                parts.append(f"{label}: {el.get_text(' ', strip=True)}")

        # Try __NEXT_DATA__ JSON for even richer data
        next_data = soup.select_one("#__NEXT_DATA__")
        if next_data:
            try:
                nd = json.loads(next_data.string or "{}")
                md_obj = (
                    nd.get("props", {}).get("pageProps", {}).get("initialData", {}).get("matchDetails")
                    or nd.get("props", {}).get("pageProps", {}).get("matchData")
                )
                if md_obj:
                    parts.append(f"NEXT_DATA_STATUS: {md_obj.get('status','')}")
                    parts.append(f"NEXT_DATA_CRR: {md_obj.get('crr','')}")
                    parts.append(f"NEXT_DATA_RRR: {md_obj.get('rrr','')}")
                    parts.append(f"NEXT_DATA_PARTNERSHIP: {md_obj.get('partnership','')}")
                    parts.append(f"NEXT_DATA_LAST_WICKET: {md_obj.get('lastWicket','')}")
            except Exception:
                pass

        result = "\n".join(parts)
        if result.strip():
            log.info(f"TOI match detail: {len(parts)} data points from {url}")
            return result
    except Exception as ex:
        log.warning(f"TOI match detail failed [{url}]: {ex}")
    return ""


# ── Source 5: NDTV Cricket (good fallback) ────────────────────────────────────
def scrape_ndtv() -> str:
    try:
        html = _get("https://sports.ndtv.com/cricket/live-scores",
                    extra_headers={"Host": "sports.ndtv.com"})
        soup = BeautifulSoup(html, "html.parser")
        blocks = []
        for card in soup.select(".sp-scr_mtch-itm")[:4]:
            text = card.get_text(" ", strip=True)
            if text:
                blocks.append(text)
        result = "\n---\n".join(blocks)
        if result.strip():
            log.info(f"NDTV: {len(blocks)} match cards")
            return result
    except Exception as ex:
        log.warning(f"NDTV failed: {ex}")
    return ""


# ══════════════════════════════════════════════════════════════════════════════
#  AGGREGATOR — try all sources, return best combined text
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all_data() -> str:
    """Try all scrapers in priority order, combine what we get."""
    chunks = []

    # Priority 1: TOI match detail (richest — has batsmen, bowlers, balls)
    toi_urls = scrape_toi_list()
    for url in toi_urls[:1]:   # take first live match
        detail = scrape_toi_match(url)
        if detail:
            chunks.append(f"=== TOI MATCH DETAIL ===\n{detail}")
            break

    # Priority 2: Cricbuzz RSS (reliable basic scores)
    rss = scrape_cricbuzz_rss()
    if rss:
        chunks.append(f"=== CRICBUZZ RSS ===\n{rss}")

    # Priority 3: Cricbuzz HTML
    if not chunks:
        cb_html = scrape_cricbuzz_html()
        if cb_html:
            chunks.append(f"=== CRICBUZZ HTML ===\n{cb_html}")

    # Priority 4: CREX
    if not chunks:
        crex = scrape_crex()
        if crex:
            chunks.append(f"=== CREX ===\n{crex}")

    # Priority 5: NDTV
    if not chunks:
        ndtv = scrape_ndtv()
        if ndtv:
            chunks.append(f"=== NDTV ===\n{ndtv}")

    combined = "\n\n".join(chunks)
    log.info(f"Total scraped text: {len(combined)} chars from {len(chunks)} source(s)")
    return combined


# ══════════════════════════════════════════════════════════════════════════════
#  GEMINI AI ENRICHMENT
# ══════════════════════════════════════════════════════════════════════════════

def ask_gemini(raw_text: str) -> dict | None:
    if not GEMINI_API_KEY:
        log.warning("GEMINI_API_KEY not set — RSS-only fallback mode")
        return None
    try:
        client  = genai.Client(api_key=GEMINI_API_KEY)
        now_utc = datetime.utcnow().strftime("%H:%M:%S UTC")
        user_msg = (
            f"Current UTC time: {now_utc}\n\n"
            f"Live cricket data from multiple sources:\n\n{raw_text[:4000]}\n\n"
            "Extract the single most interesting LIVE match and return the JSON."
        )
        resp = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=user_msg,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.1,
                max_output_tokens=1400,
            ),
        )
        raw = resp.text.strip()
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw).strip()
        data = json.loads(raw)
        log.info(
            f"Gemini → {data.get('team1',{}).get('name','?')} vs "
            f"{data.get('team2',{}).get('name','?')}  |  "
            f"{data.get('match_status','')[:60]}"
        )
        return data
    except json.JSONDecodeError as e:
        log.error(f"Gemini JSON error: {e}")
    except Exception as e:
        log.error(f"Gemini error: {e}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  REGEX FALLBACK PARSER (when Gemini unavailable)
# ══════════════════════════════════════════════════════════════════════════════

def parse_fallback(raw_text: str) -> dict:
    data = json.loads(json.dumps(FALLBACK))
    data["last_updated"] = datetime.utcnow().strftime("%H:%M:%S UTC")
    if not raw_text:
        return data

    # Team names from "X vs Y" pattern
    m = re.search(r"([A-Z][A-Za-z ]{1,25})\s+vs?\s+([A-Z][A-Za-z ]{1,25})", raw_text)
    if m:
        data["team1"]["name"] = m.group(1).strip()[:20]
        data["team2"]["name"] = m.group(2).strip()[:20]

    # Score like 182/4
    m = re.search(r"(\d{1,3}/\d{1})\s*(?:\((\d{1,2}(?:\.\d)?)\s*(?:ov)?\))?", raw_text)
    if m:
        data["team1"]["score"] = m.group(1)
        if m.group(2):
            data["team1"]["overs"] = m.group(2)

    # CRR
    m = re.search(r"CRR[:\s]+(\d+\.\d+)", raw_text, re.I)
    if m:
        data["crr"] = m.group(1)

    # RRR
    m = re.search(r"RRR[:\s]+(\d+\.\d+)", raw_text, re.I)
    if m:
        data["rrr"] = m.group(1)

    # Batsman
    m = re.search(r"BATSMAN:\s*(.+?)\s+R:(\d+)\s+B:(\d+).*?4s:(\d+).*?6s:(\d+).*?SR:([\d.]+).*?STRIKER:(True|False)", raw_text)
    if m:
        data["batsman1"] = {
            "name": m.group(1), "runs": int(m.group(2)), "balls": int(m.group(3)),
            "fours": int(m.group(4)), "sixes": int(m.group(5)), "sr": m.group(6),
            "on_strike": m.group(7) == "True", "photo": ""
        }

    # Bowler
    m = re.search(r"BOWLER:\s*(.+?)\s+O:([\d.]+)\s+M:(\d+)\s+R:(\d+)\s+W:(\d+)\s+ECO:([\d.]+)", raw_text)
    if m:
        data["bowler"] = {
            "name": m.group(1), "overs": m.group(2), "maidens": int(m.group(3)),
            "runs": int(m.group(4)), "wickets": int(m.group(5)), "economy": m.group(6), "photo": ""
        }

    # Recent balls
    m = re.search(r"(?:RECENT_BALLS|THIS_OVER):\s*([\d W.]+)", raw_text)
    if m:
        balls = m.group(1).split()[-6:]
        data["last_over_balls"] = balls
        if balls:
            data["current_ball"] = balls[-1]

    # Status
    m = re.search(r"STATUS:\s*(.+)", raw_text)
    if m:
        data["match_status"] = m.group(1).strip()[:80]

    return data


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN POLL LOOP
# ══════════════════════════════════════════════════════════════════════════════

_last_raw_hash = ""   # only call Gemini when content changes

def update_data_json():
    global _last_raw_hash

    raw_text = fetch_all_data()

    if not raw_text:
        log.warning("No data from any source — keeping existing data.json")
        return

    # Hash raw text — only call Gemini if content actually changed
    raw_hash = hashlib.md5(raw_text.encode()).hexdigest()
    if raw_hash == _last_raw_hash:
        log.info("Content unchanged — skipping Gemini call, data.json unchanged")
        return
    _last_raw_hash = raw_hash

    # Try Gemini first
    enriched = ask_gemini(raw_text)
    if enriched:
        enriched.setdefault("last_updated", datetime.utcnow().strftime("%H:%M:%S UTC"))
        out = enriched
    else:
        log.info("Gemini unavailable — using regex fallback parser")
        out = parse_fallback(raw_text)

    DATA_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    log.info("data.json written ✓")


def poll_loop():
    while True:
        try:
            update_data_json()
        except Exception as e:
            log.error(f"Poll error: {e}")
        time.sleep(POLL_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  HTTP SERVER
# ══════════════════════════════════════════════════════════════════════════════

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)
    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not HTML_FILE.exists():
        log.error(f"livematch.html not found in {BASE_DIR}")
        raise SystemExit(1)

    log.info("=" * 50)
    log.info("  Cricket Live Overlay — Final Server")
    log.info("=" * 50)
    log.info(f"  Gemini  : {'KEY SET ✓' if GEMINI_API_KEY else 'NOT SET — fallback mode'}")
    log.info(f"  Poll    : every {POLL_INTERVAL}s (Gemini called only on change)")
    log.info(f"  Port    : {PORT}")
    log.info(f"  Sources : TOI detail + Cricbuzz RSS/HTML + CREX + NDTV")

    # Write initial data
    log.info("Initial fetch...")
    try:
        update_data_json()
    except Exception as e:
        log.warning(f"Initial fetch failed ({e}) — writing placeholder")
        out = json.loads(json.dumps(FALLBACK))
        out["last_updated"] = datetime.utcnow().strftime("%H:%M:%S UTC")
        DATA_FILE.write_text(json.dumps(out, indent=2))

    threading.Thread(target=poll_loop, daemon=True).start()

    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("")
    log.info(f"  Local  → http://localhost:{PORT}/livematch.html")
    log.info(f"  Railway→ https://your-app.up.railway.app/livematch.html")
    log.info("")
    httpd.serve_forever()
