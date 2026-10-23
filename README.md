# AKDash

AKDash turns Excel, CSV and **PDF reports** into detailed, interactive dashboards. The AI designs the
dashboard from a data profile and the user's persona, and the app calculates every number with DuckDB.

## Setup

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

Copy `.streamlit/secrets.toml.example` to `.streamlit/secrets.toml` and fill it in. On Render, set the same
names as Environment Variables. `secrets.toml` is in `.gitignore`, so keys are never committed.

### Choosing the AI (open-source models supported)

AKDash works with any OpenAI-compatible API, so you can run it on **free, open-source models**:

| `provider` | Models (defaults) | Key | Cost |
|---|---|---|---|
| `gemini` | Google's open **Gemma 4** models (`gemma-4-31b-it`) | `GEMINI_API_KEY` from aistudio.google.com | Free tier |
| `groq` | `openai/gpt-oss-120b`, Qwen | `GROQ_API_KEY` from console.groq.com | Free tier (small daily token cap) |
| `openrouter` | `openai/gpt-oss-120b:free` and other `:free` models | `OPENROUTER_API_KEY` from openrouter.ai | Free models; the daily limit rises after a one-time $10 credit |
| `ollama` | Any model you pull, e.g. `qwen3:14b`, `gpt-oss:20b` | None | Free and private; needs a capable machine |
| `openai_compatible` | vLLM, LM Studio, etc. (`base_url` + `model`) | `LLM_API_KEY` | Depends |
| `mistral` / `anthropic` | Mistral / Claude models | `MISTRAL_API_KEY` / `ANTHROPIC_API_KEY` | Paid / free tiers |

- **Free-tier models are picked automatically.** Each provider has a list of free models:
  - **Mistral:** `ministral-14b-latest` → `ministral-8b-latest` → `open-mistral-nemo`
  - **Gemini:** `gemma-4-31b-it` → `gemma-4-26b-a4b-it` → `gemma-3-27b-it`
  - **Groq:** `openai/gpt-oss-120b` → `qwen/qwen3.6-27b` → `openai/gpt-oss-20b`

  If your key's plan doesn't offer a model, the next one is used. Run `python check_ai.py` to see which models work with your keys.
- **Backup chain:** the main `provider` is tried first. When it is rate-limited, out of quota, unreachable, or the request is too large for its free tier, the next provider answers.
  - **Default order:** Mistral → Gemini (Gemma) → Groq → OpenRouter. Only providers with a key are used, so adding `MISTRAL_API_KEY`, `GEMINI_API_KEY` and `GROQ_API_KEY` is enough.
  - **Custom order:** set `fallback_providers = "gemini,groq"`. Backup models are set with `gemini_model`, `groq_model` and so on.
  - **Groq:** its free tier only fits requests up to about 8,000 tokens. Bigger requests (designing a dashboard) skip Groq without calling it, while small ones (chat answers, query fixes) can use it.
  - **Gemma on Google's API** doesn't accept system messages, so the instructions are sent in the user message automatically.
  - **Visibility:** the app shows which AI answered, for example "AI: Mistral → Google AI Studio".
- **Output format:** the app asks for JSON-schema output first, falls back to JSON mode, and finally to plain-prompt JSON. This lets models without structured-output support still work.

```toml
provider = "mistral"
model = "ministral-14b-latest"
MISTRAL_API_KEY = "your-mistral-key"
GEMINI_API_KEY = "your-gemini-key"   # backup 1 (Gemma)
GROQ_API_KEY = "your-groq-key"       # backup 2 (gpt-oss-120b)
signup_code = ""                     # optional: a code required to create new accounts
```

**Privacy:** free tiers may log prompts or use them to improve the provider's models. The AI sees the data profile, sample rows and aggregated results. For confidential company data, use a paid tier with a no-training policy, or `ollama` on your own server.

## Run

```bash
streamlit run app.py
```

## Accounts, profiles and personas

- **Register / log in / log out.** Passwords are stored as salted PBKDF2-SHA256 hashes.
- **The first account becomes the administrator.** It also takes over any projects created before accounts existed.
- **Keep me logged in (7 days).** A random token is stored in a cookie, and only its hash is kept in the database. Logging out, changing the password or disabling the account invalidates it.
- **Sign-up code (optional).** Set `signup_code` in `secrets.toml` to require a code for new accounts. The first account never needs one.
- **My profile** has three tabs:
  - **Persona:** job title, department, organization, responsibilities, key metrics, language and level of detail. The persona is added to **every** AI prompt (dashboard design, insights, chat, PDF reading), so the output is written for that person. A preview shows exactly what the AI is told.
  - **Account:** name and email.
  - **Password:** changing it logs out your other devices.
- **Users** (admins only): see all users, change roles, activate or deactivate accounts, and reset passwords. The last admin can't be demoted.
- **Private projects:** each user sees only their own projects.

## Uploading data: Excel, CSV and PDF

Upload one or more `.xlsx`, `.csv` or `.pdf` files. You can mix them in one upload.

- **Excel and CSV:** every sheet is cleaned. Title rows, total rows and repeated headers are removed, numbers stored as text are converted, and Excel dates and month text ("2026-07", "Jul 2026") become dates.
- **PDF reports with tables:** tables are read with `pdfplumber`. A table that continues on the next page (same columns, repeated header) is joined into one table, then cleaned like a sheet.
- **PDF reports with figures only in the text:** the AI copies the figures into tables, word for word. The app then cleans the data and calculates on it like a sheet. These tables are marked *"extracted by AI, please check the numbers"* in the data preview.
- **PDF report text:** a short excerpt from each PDF goes to the AI as context for the insights.
- **Scanned PDFs:** these have no text layer and aren't supported. The app shows a warning.

## How the dashboard is built

1. `profiler.py` builds a **data profile**: columns, types, null counts, min/max, top values, 10 sample rows and PDF text excerpts. The full data is never sent to the AI.
2. The AI returns a **detailed Dashboard Spec**, every time. The target is 22–30 widgets:
   - **Overview:** 6–8 KPIs.
   - **Trends:** growth % and cumulative totals.
   - **Breakdown:** every category, shares, top/bottom 10 and a Pareto table.
   - **Exceptions:** outliers, negatives and missing values.
3. **Detail check:** if the result is still thin, the app asks the AI once more for the missing widgets.
4. `engine.py` checks that each query is a single read-only `SELECT`, then runs it with DuckDB on the full data, with file access disabled. Failing queries go back to the AI to fix (2 attempts at most). Widgets that still fail are left out of a new dashboard and listed for the user.
5. Only the **aggregated results** go back to the AI for the insights, so the insights quote the computed numbers.

## Chat, versions and daily updates

- **Chat** changes the dashboard ("add sales by region") or answers questions with SQL run on the full data. Each request sends the AI the profile, the original instructions, the current spec and the last 10 messages.
- **Versions:** every spec version and chat message is saved. **Revert** saves an older version again as the newest one, so nothing is ever deleted.
- **Daily upload:**
  - The dashboard refreshes from the saved spec.
  - If columns changed, you map old names to new ones. Mappings are remembered for later uploads.
  - **Replace** or **Append** mode controls how new files are saved.
  - The insights are rewritten, including "What changed since the last upload".

## Look and feel

- **Branding:** the AKDash logo (`assets/logo.svg`) is an original mark: dashboard bars with a gold thread weaving through them, on textile indigo. The full logo with the wordmark is in `assets/logo_full.svg`, with a dark-mode version in `assets/logo_full_dark.svg`.
- **Themes:** light and dark are separate palettes (`.streamlit/config.toml`). Charts use a palette checked for colour-blind readers, and each category keeps its colour when filters change.
- **Motion:** animations switch off automatically for users who choose "reduce motion" in their system settings.

## Sample data

```bash
python samples/make_samples.py
```

This creates messy Excel/CSV files and two PDF reports in `samples/`:

| File | What it tests |
|---|---|
| `sales_report.xlsx` | Title rows, an empty column, amounts typed as text, `Total` / `Grand Total` rows, two sheets |
| `monthly_sales.xlsx` | `Jan`/`Feb`/`Mar` sheets combined into one table, Excel serial dates, `Subtotal` rows |
| `expenses.csv` | A title row, `dd/mm/yyyy` dates, amounts with commas, `-` placeholders, a `Grand Total` row |
| `store_sales_q3.pdf` | A bordered table spanning two pages (header repeated), a total row, `YYYY-MM` months |
| `management_summary_sep.pdf` | Figures only in sentences, so the AI table extraction is used |

## Tests

```bash
python -m pytest -q
```

The tests cover:
- **Cleaning:** checks against the sample files.
- **Query safety:** SELECT-only checks.
- **Numbers:** KPI values compared with plain pandas.
- **AI flows with a fake AI:** the query-fix loop, the detail top-up, chat and refreshed insights.
- **Data storage:** versions and revert, Replace/Append, column mapping.
- **Accounts:** password hashing, registration rules, admin hand-over, private projects, persona prompts.
- **PDF parsing:** tables across pages and AI extraction.
- **Mistral client:** tested with a mocked HTTP layer.

## Where things are stored

- `app.db` (SQLite): users, login sessions, projects, uploads, chat messages, dashboard spec versions and column mappings. Override the location with the `AKDASH_DB` environment variable.
- `data/<project_id>/<date>_<filename>`: the original uploaded files. Override with `AKDASH_DATA`.
- `data/<project_id>/upload_<id>/`: the cleaned tables as `.parquet` files, plus `documents.json` with the PDF text.
