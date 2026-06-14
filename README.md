# Hacker News — Read The Room

FastHTML + MonsterUI web app that validates a Hacker News item ID/URL, fetches the discussion, summarizes the comment-room sentiment through OpenRouter free 1M-context models, caches markdown summaries in SQLite, and renders them cleanly.

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://localhost:8000

## Notes

- Accepts `/item?id=XXXXXXXX` and homepage form input.
- Strictly accepts numeric HN IDs or `news.ycombinator.com/item?id=...` URLs.
- Validates IDs against the official HN Firebase API.
- Uses `llm-hacker-news` comment processing against Algolia's HN item API.
- Discovers suitable OpenRouter models daily from `/api/v1/models`:
  - free prompt/completion pricing
  - text-to-text
  - effective context length >= 1M
- Caches summaries and model metadata in `readtheroom.db`.
