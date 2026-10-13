# AI Competitor Watch Agent

An automated competitive intelligence agent that monitors 5–10 companies across multiple data sources, filters noise, deduplicates coverage, scores strategic importance, and compiles executive briefs in HTML, Markdown, and JSON formats.

---

## Architecture & Pipeline

Competitor Watch operates as a multi-stage pipeline:

```
[ Google News RSS ] ──┐
[ Company Feeds   ] ──┼─> [ COLLECT ] ─> [ NOISE FILTER ] ─> [ DEDUPE ] ─> [ CLASSIFY & SCORE ] ─> [ EXECUTIVE SUMMARY ] ─> [ RENDER ]
[ Page Watcher    ] ──┘                                                          (LLM or Rules)         (LLM or Rules)        ├── brief.html
                                                                                                                              ├── brief.md
                                                                                                                              └── updates.json
```

1. **Collect**: Gathers items across three channels:
   - **Google News RSS**: Multi-angle search queries (releases, pricing, leadership, partnerships).
   - **Official Company Feeds**: Direct RSS/Atom feeds configured in `sources.json`.
   - **Page Watcher**: Text extraction and differential change detection on pricing and newsroom pages.
2. **Noise Filter**: Drops items older than the lookback window, removes low-quality domains and financial clickbait patterns, and verifies company mentions. First-party updates bypass topical heuristics.
3. **Deduplicate**: Groups cross-outlet coverage of identical events using Jaccard token similarity; prioritizes official sources as canonical.
4. **Classify & Score**: Categorizes events into `product`, `pricing`, `hiring`, `partnership`, `funding_mna`, `legal_regulatory`, `security_outage`, or `other`, scoring strategic importance from 1 (trivia) to 5 (strategic/urgent).
5. **Executive Summary**: Synthesizes a high-level "Bottom line" assessment, top strategic signals with "So what" implications, and a "Watch next" radar.
6. **Render & Deliver**: Generates an interactive modern HTML brief, clean GitHub-flavored Markdown brief, structured JSON data, and optional Slack notifications.

---

## Project Structure

```
competitor-watch/
├── .github/
│   └── workflows/
│       └── weekly.yml         # Automated GitHub Actions weekly schedule & artifact upload
├── .env.example               # Template for environment variables
├── .gitignore                 # Excludes caches, venvs, logs, local state, and generated reports
├── competitor_watch.py        # Core intelligence agent application (Standard Library)
├── README.md                  # Project documentation and operational guide
├── requirements.txt           # Optional package specifications (e.g., Anthropic SDK)
├── run_weekly.ps1             # PowerShell runner for Windows Task Scheduler
└── sources.json               # Configured targets, feeds, and watched web pages
```

---

## Quick Start

### 1. Prerequisites
- **Python 3.10+** (Tested on Python 3.10 through 3.14).
- The core application relies **exclusively on the Python Standard Library**. No external packages are required for heuristic mode or Google Gemini.

### 2. (Optional) Dependencies
If you prefer using Anthropic Claude instead of Google Gemini:
```powershell
pip install -r requirements.txt
```

### 3. Environment Variables
Configure your chosen LLM provider and delivery webhooks. You can set them in your environment or copy `.env.example` to `.env`:

| Variable | Required | Description |
|---|---|---|
| `GEMINI_API_KEY` | Optional | Free API key from [Google AI Studio](https://aistudio.google.com). Requires no third-party libraries. |
| `ANTHROPIC_API_KEY` | Optional | Paid API key from [Anthropic Console](https://console.anthropic.com). Requires `pip install anthropic`. |
| `GEMINI_MODEL` | Optional | Model identifier for Gemini (default: `gemini-flash-latest`). |
| `WATCH_MODEL` | Optional | Model identifier for Claude (default: `claude-sonnet-5-5`). |
| `SLACK_WEBHOOK_URL` | Optional | Incoming Slack webhook URL to post top signals upon completion. |

> **Note**: If neither `GEMINI_API_KEY` nor `ANTHROPIC_API_KEY` is provided, the tool automatically falls back to deterministic heuristic classification and ranking.

---

## Source Verification

Verify that feeds and watched pages configured in `sources.json` are active and responsive:

```powershell
python competitor_watch.py --check-sources
```

Output indicators:
- `OK`: Endpoint responded successfully with parsed entries or sufficient text.
- `WARN`: Page returned very little text (likely client-side JavaScript rendered) or feed returned 0 entries.
- `FAIL`: URL could not be retrieved or encountered an HTTP error.

### Customizing `sources.json` (Adding & Removing Companies)

The application dynamically reads company targets, aliases, RSS/Atom feeds, and watched web pages from `sources.json`. You can add, edit, or remove companies entirely through `sources.json` without modifying any Python code.

- **To add a company**: Add a new top-level key matching the company name.
- **To remove a company**: Delete or comment out the company key from `sources.json`.
- **Validation**: Feeds must be HTTP(S) URL strings; pages must include `url` (and optional `label`, `category`, and `ignore_regex`).
- **Reports adaptation**: All generated briefs (`brief.html`, `brief.md`, `updates.json`) dynamically generate sections and metrics matching only the configured companies.

Example entry:
```json
{
  "Anthropic": {
    "aliases": ["Claude"],
    "feeds": [],
    "pages": [
      {"url": "https://www.anthropic.com/news", "label": "Newsroom", "category": "company_announcement"},
      {"url": "https://claude.com/pricing", "label": "Claude pricing", "category": "pricing"}
    ]
  }
}
```

---

## Running the Agent

### Ad-hoc run with custom companies
```powershell
python competitor_watch.py "OpenAI" "Anthropic" "Mistral AI" "Cohere" "Perplexity" --days 7 --min-importance 2 --open
```

### Run using targets defined in `sources.json`
```powershell
python competitor_watch.py --days 7 --open
```

### Offline sample demo (no network / no API keys required)
```powershell
python competitor_watch.py --demo --no-llm --open
```

### Command-Line Options

| Flag | Default | Description |
|---|---|---|
| `companies` | from `sources.json` | 1–10 target company names (syntax: `"Company\|alias1\|alias2"`). |
| `--days` | `7` | Number of lookback days for news and feed items. |
| `--min-importance` | `2` | Minimum importance score (1–5) included in final report. |
| `--per-company` | `25` | Max news stories per company passed to the LLM (controls cost). |
| `--sources` | `sources.json` | Path to sources configuration file. |
| `--state` | `state/` | Directory where page watcher baselines are stored. |
| `--out` | `reports/<YYYY-MM-DD>` | Output directory for generated brief files. |
| `--no-llm` | `False` | Force heuristic classification and rule-based summarization. |
| `--demo` | `False` | Run offline simulation using built-in mock events. |
| `--check-sources` | `False` | Validate feeds and pages in `sources.json` and exit. |
| `--open` | `False` | Automatically launch `brief.html` in your default browser. |
| `--print` | `False` | Print the generated Markdown brief directly to stdout. |

### Generated Outputs
Outputs are saved into `reports/<YYYY-MM-DD>/`:
- `brief.html`: Interactive, responsive report with KPI badges, company accordions, and dark mode support.
- `brief.md`: Clean Markdown report ready for email or team wikis.
- `updates.json`: Raw structured dataset containing deduplicated and scored items.

---

## Scheduling

### Windows Task Scheduler (via `run_weekly.ps1`)
The included `run_weekly.ps1` script manages working directory resolution, optional `.env` loading, Python execution, and output logging into `run.log`.

1. Place the repository in a persistent folder (e.g. `C:\tools\competitor-watch`).
2. Set your environment variable once:
   ```cmd
   setx GEMINI_API_KEY "your-gemini-key-here"
   ```
3. Register the weekly scheduled task (e.g., every Monday at 09:00 AM):
   ```cmd
   schtasks /Create /SC WEEKLY /D MON /ST 09:00 /TN "CompetitorWatch" /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\tools\competitor-watch\run_weekly.ps1"
   ```
4. Test the scheduled task:
   ```cmd
   schtasks /Run /TN "CompetitorWatch"
   ```
5. Check `run.log` in the project directory for execution logs.

---

## GitHub Actions Configuration

The project includes an automated GitHub Actions workflow at `.github/workflows/weekly.yml` that runs on a weekly schedule without requiring a local workstation to be on.

### Setup Steps:
1. Push this repository to GitHub (recommended: **Private** repository).
2. Go to repository **Settings** > **Secrets and variables** > **Actions**.
3. Add the following repository secrets:
   - `GEMINI_API_KEY`: Your Gemini API key.
   - `ANTHROPIC_API_KEY`: (Optional) Your Anthropic API key if using Claude.
### Manual Execution:
You can trigger the GitHub Actions workflow at any time without waiting for the Monday cron schedule:

**Via GitHub Web Interface:**
1. Navigate to your repository on GitHub: `https://github.com/YAP545/competitor-watch`.
2. Click the **Actions** tab at the top.
3. In the left sidebar, select **Weekly Competitor Watch**.
4. Click the **Run workflow** dropdown button on the right.
5. (Optional) Customize the lookback window (default: `7` days) and minimum importance filter (default: `2`).
6. Click **Run workflow**.

**Via GitHub CLI (`gh`):**
```powershell
gh workflow run weekly.yml --repo YAP545/competitor-watch -f days=7 -f min_importance=2
```

---

## Cloud Infrastructure & GCP Requirements

**No Google Cloud Platform (GCP) or paid cloud infrastructure is required.**
- The AI summarization pipeline connects directly to Google AI Studio's Gemini REST API via the Python Standard Library (`urllib.request`).
- No Google Cloud Console project, BigQuery, Vertex AI, Google Cloud Storage, or GCP billing activation is required.
- Everything runs either locally (via PowerShell / Task Scheduler) or in free GitHub Actions runner minutes.

---

## Privacy & Security

- **Secrets Management**: Never commit `.env`, API keys, or webhooks. All keys are resolved via environment variables.
- **Ignored Files**: The `.gitignore` file automatically excludes `.env`, `run.log`, `state/` (local monitoring baselines), and `reports/` (generated briefs).
