# LinkedIn Automation

This project reads a public CSV of ideas, picks the next unused item, generates a LinkedIn post with Claude, creates a matching image with OpenAI, and publishes to LinkedIn through the official API.

## Features

- Reads a public CSV URL
- Tracks processed rows in a local CSV to avoid duplicates
- Generates a LinkedIn post with Anthropic Claude
- Creates a matching image with OpenAI Images
- Publishes to LinkedIn using OAuth and UGC endpoints
- Supports dry-run mode for testing and review

## Local setup

1. Create a virtual environment:
   python -m venv .venv
   .\.venv\Scripts\activate

2. Install dependencies:
   pip install -r requirements.txt

3. Copy `.env.example` to `.env` and fill in your credentials.

4. Run the app in dry-run mode:
   python main.py --url "https://drive.google.com/uc?export=download&id=YOUR_FILE_ID" --dry-run

5. Run real publish mode when ready:
   python main.py --url "https://drive.google.com/uc?export=download&id=YOUR_FILE_ID" --no-dry-run

## GitHub Actions

The workflow in `.github/workflows/linkedin-automation.yml` runs this app automatically with GitHub secrets.

Required repository secrets:
- ANTHROPIC_API_KEY
- OPENAI_API_KEY
- LINKEDIN_CLIENT_ID
- LINKEDIN_CLIENT_SECRET
- LINKEDIN_ACCESS_TOKEN
- LINKEDIN_REDIRECT_URI
- PUBLIC_CSV_URL

## Important notes

- The scheduled GitHub Actions workflow publishes live by default. Use the manual `dry_run=true` option to test without publishing.
- Every workflow run downloads a fresh copy from `PUBLIC_CSV_URL`, then merges local statuses, post IDs, and generated metadata into `Ideas_marked.csv`.
- The workflow commits the updated tracking CSV after each run so processed ideas are not selected again.
