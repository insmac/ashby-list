# Ashby Job Scraper

Scrapes Google search results for job postings on [jobs.ashbyhq.com](https://jobs.ashbyhq.com) and stores them in a [Neon](https://neon.tech) PostgreSQL database. Uses Playwright to drive a real Chrome browser, which avoids CAPTCHAs when paired with a logged-in Chrome profile.

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — fast Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Google Chrome** installed
- **Neon PostgreSQL** database (free tier works fine) — [neon.tech](https://neon.tech)

## Setup

### 1. Clone & install dependencies

```bash
git clone <repo-url> && cd ashby-list
uv sync                           # creates venv + installs all deps
uv run playwright install chromium # downloads the browser binary
```

### 2. Create a dedicated Chrome profile

Using a separate Chrome profile is **strongly recommended**. A profile that has Google cookies / search history behaves like a real user and almost never triggers CAPTCHAs.

#### macOS / Linux

1. Open Chrome.
2. Click your profile avatar (top-right) → **Add** → create a new profile (e.g. "Scraper").
3. In the new profile window, sign in to Google (optional, but helps).
4. Visit `chrome://version` and note the **Profile Path**, e.g.:
   ```
   /Users/you/Library/Application Support/Google/Chrome/Profile 4
   ```
5. The **User Data Dir** is everything before the profile folder:
   ```
   /Users/you/Library/Application Support/Google/Chrome
   ```
   The **Profile** name is the last segment: `Profile 4`.

#### Windows

Same steps, but the typical paths are:

```
User Data Dir: C:\Users\you\AppData\Local\Google\Chrome\User Data
Profile:       Profile 4
```

> **Important:** Close Chrome completely before running the scraper — Playwright cannot attach to a profile that is already in use.

### 3. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in the values:

| Variable               | Required | Description                                                |
| ---------------------- | -------- | ---------------------------------------------------------- |
| `DATABASE_URL`         | ✅       | Neon PostgreSQL connection string                          |
| `CHROME_USER_DATA_DIR` | ✅       | Path to Chrome's user data directory (see step 2)          |
| `CHROME_PROFILE`       | —        | Profile folder name, defaults to `Default`                 |
| `SEARCH_QUERY`         | —        | Custom Google search query (has a built-in default)        |
| `BROWSERLESS_URL`      | —        | Browserless.io WebSocket URL (alternative to local Chrome) |

The default search query is:

```
site:jobs.ashbyhq.com ("front-end" OR "frontend" OR "fullstack" OR "product") remote
```

Override `SEARCH_QUERY` in `.env` to search for different roles.

## Usage

```bash
uv run python job_scraper.py
```

The scraper will:

1. Open Chrome with your profile.
2. Search Google for Ashby job listings.
3. Paginate through up to 10 pages of results.
4. Store new jobs in the database and print today's discoveries.

## Troubleshooting

| Problem                              | Fix                                                                              |
| ------------------------------------ | -------------------------------------------------------------------------------- |
| CAPTCHA / "unusual traffic"          | Use a Chrome profile that has Google cookies (step 2). Don't run too frequently. |
| `error: Could not connect to Chrome` | Make sure Chrome is **fully closed** before running the scraper.                 |
| `DATABASE_URL not set`               | Copy `.env.example` to `.env` and fill in your Neon connection string.           |
| No results found                     | Check `debug_search.png` — it's a screenshot of what the scraper saw.            |

## License

MIT
