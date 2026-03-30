# 🏏 Cricket Live Overlay

Live cricket score overlay for OBS Studio. Auto-fetches data every 90 seconds
from multiple sources and updates `livematch.html` via `data.json`.

## How it works

```
TOI match-center (batsmen, bowlers, balls, CRR, RRR)   ← richest
Cricbuzz RSS  (reliable basic scores)
Cricbuzz HTML (live score cards)
CREX.live     (fallback)
NDTV Cricket  (fallback)
       ↓  combined text
   Gemini 2.0 Flash  → structured JSON
       ↓  (only called when content changes — saves free quota)
    data.json   (written every 90s)
       ↑  polled every 30s
  livematch.html  →  OBS overlay
```

## Repo structure

```
cricket-live-overlay/
├── server.py          ← Python backend
├── livematch.html     ← OBS overlay (1280×720)
├── requirements.txt   ← Python deps
├── Procfile           ← Railway start command
├── runtime.txt        ← Python version
├── .gitignore
└── README.md
```

## Local setup

```bash
# 1. Install deps
pip install -r requirements.txt

# 2. Set Gemini API key (free at https://aistudio.google.com/app/apikey)
export GEMINI_API_KEY="AIza..."      # Mac/Linux
$env:GEMINI_API_KEY="AIza..."        # Windows PowerShell

# 3. Run
python server.py
```

Open: **http://localhost:8080/livematch.html**

**OBS Browser Source:** `http://localhost:8080/livematch.html` | 1280×720

## Deploy to Railway

1. Push this repo to GitHub
2. railway.com → New Project → Deploy from GitHub repo
3. Variables tab → add `GEMINI_API_KEY = AIza...`
4. Your public URL → use as OBS Browser Source

Railway reads the `PORT` env var automatically. Free within $5/month credit.

## Gemini free tier

Gemini 2.0 Flash free limit: **1,000 requests/day**.

This server calls Gemini **only when scraped content changes** — during a
live match roughly every 90s = ~960 calls/day. On days with no live cricket,
almost zero calls are made.

## data.json schema

```json
{
  "team1":        { "name": "India", "score": "182/4", "overs": "19.2" },
  "team2":        { "name": "Australia", "score": "---", "overs": "" },
  "match_status": "IND need 43 off 24 balls",
  "crr": "8.45", "rrr": "10.75", "target": "225",
  "partnership":  "42(28)", "need": "43 runs in 24 balls",
  "last_wicket":  "Kohli 45(38) b Starc",
  "current_over": 19, "current_ball": "4",
  "last_over_balls": ["1","0","4","W","2","6"],
  "batsman1": { "name":"Rohit","runs":78,"balls":52,"fours":8,"sixes":3,"sr":"150.00","on_strike":true },
  "batsman2": { "name":"Hardik","runs":34,"balls":22,"fours":3,"sixes":2,"sr":"154.54","on_strike":false },
  "bowler":   { "name":"Starc","wickets":2,"runs":38,"overs":"3.2","maidens":0,"economy":"11.40" },
  "venue": { "name": "Melbourne Cricket Ground" },
  "last_updated": "14:35:22 UTC"
}
```

## Manual override

Click the ⚙️ gear icon on the overlay to manually set any field.
