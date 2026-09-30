"""
Telegram JOBS channel -> LinkedIn (text only, in the fixed job-post template).

Every run:
  1. Reads new posts from the Jobs Telegram channel with the bot (TBOT_KEY).
  2. Uses OpenAI (OPENAI_API_KEY) to rewrite each post into the fixed template
     below - only facts found in the post, nothing invented, links kept as-is.
  3. Publishes it on LinkedIn as a TEXT-ONLY post, with the fixed footer.
  4. Optionally replies in the channel with the LinkedIn URL.

The bot must be an ADMIN of the Jobs channel. Processed ids are kept in
telegram_jobs_state.json so nothing is posted twice.

Usage:
  python telegram_jobs.py            # live
  python telegram_jobs.py --dry-run  # log only, no LinkedIn / no replies
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import requests

from linkedin_automation.linkedin_service import create_ugc_post, get_member_urn
from linkedin_automation.utils_logger import setup_logger

logger = setup_logger("telegram_jobs")

DEFAULT_JOBS_CHAT_ID = "-1004296140869"
STATE_FILE = "telegram_jobs_state.json"
MIN_CHARS = 15
LINKEDIN_LIMIT = 2900  # LinkedIn hard limit is 3000
OPENAI_MODEL = os.environ.get("JOBS_OPENAI_MODEL", "gpt-4o")

DEFAULT_FOOTER = (
    "To know more and download resources visit:\n"
    "https://abhijithkjayan.bolt.host/"
)

TEMPLATE = """\
🚨 JOB OPPORTUNITY | [JOB TITLE]
📍 Location: [City, Country]
🏢 Company / Recruiter: [Company / Platform]
💼 Employment Type: [Permanent / Contract / Full-time]
🎯 Experience: [Required Experience]
A new opportunity is available for professionals with experience in:
🔹 [Key Skill 1]
🔹 [Key Skill 2]
🔹 [Key Skill 3]
🔹 [Key Skill 4]
🔹 [Key Skill 5]
🎯 Role Highlights
• [Responsibility / requirement 1]
• [Responsibility / requirement 2]
• [Responsibility / requirement 3]
• [Responsibility / requirement 4]
📄 Candidate Profile
This role may be relevant for professionals with a background in [relevant function/industry], particularly those experienced in [2-4 relevant areas].
📩 Application:
[Application Link]
📧 Email: [Recruitment Email]
Subject: Application – [Job Title]
💡 If you know someone who fits this opportunity, feel free to tag them or share this post.
🔄 Sharing opportunities can help someone in your network discover their next career move.
#Hiring #JobAlert #UAEJobs #DubaiJobs #AbuDhabiJobs #[Function] #[Industry] #CareerOpportunity #FinanceJobs
"""

SYSTEM_PROMPT = f"""You convert a raw job post from a Telegram channel into a LinkedIn post that follows this exact template:

{TEMPLATE}
Rules:
- Keep the template's emojis, line order, headings and wording. Replace only the [bracketed] parts.
- Use ONLY facts present in the raw post. Never invent a company, salary, email, link or location.
- If a detail is missing: omit the Experience line, the Email line and its Subject line if no email is given; if no application link is given, omit the Application line and link. Location/Company/Employment Type may be omitted the same way if truly absent. Use 3-5 skills and 3-4 highlights, fewer if the post has fewer.
- Copy every URL and email exactly as given. Keep the raw post's main application link.
- Replace [Function] and [Industry] hashtags with single-word CamelCase tags that fit the job (e.g. #Accounting #Banking). Keep the other hashtags.
- If the raw post mentions a non-UAE location, replace #UAEJobs #DubaiJobs #AbuDhabiJobs with fitting location hashtags.
- Do NOT add the "To know more..." footer or any other link - it is added automatically.
- Output only the final post text, no commentary, no code fences, no markdown bold.
- If the raw post is not a job opening, output exactly: NOT_A_JOB"""


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("last_update_id", None)
    state.setdefault("processed", {})
    return state


def save_state(state: dict) -> None:
    state["processed"] = dict(list(state["processed"].items())[-300:])
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **params):
        r = requests.post(f"{self.base}/{method}", data=params, timeout=60)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"Telegram {method}: HTTP {r.status_code} {r.text[:200]}")
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method}: {j.get('description')}")
        return j.get("result")

    def get_updates(self, offset=None):
        params = {"timeout": 0, "allowed_updates": json.dumps(["channel_post"])}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params) or []

    def reply(self, chat_id, text, reply_to):
        self.call("sendMessage", chat_id=chat_id, text=text[:4000], reply_to_message_id=reply_to,
                  allow_sending_without_reply="true", disable_web_page_preview="true")


def extract_text(msg: dict) -> str:
    """Text/caption plus any hidden link targets, so no 'apply/join' link is lost."""
    text = (msg.get("text") or msg.get("caption") or "").strip()
    missing = []
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        url = ent.get("url")
        if url and url not in text and url not in missing:
            missing.append(url)
    if missing:
        text += "\n\n" + "\n".join(missing)
    return text.strip()


def format_job_post(raw: str) -> str | None:
    """Rewrite raw Telegram text into the template with OpenAI. None = not a job."""
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY secret is missing")
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"model": OPENAI_MODEL, "temperature": 0.3,
              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                           {"role": "user", "content": f"Raw job post:\n\n{raw}"}]},
        timeout=90,
    )
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI API HTTP {r.status_code}: {r.text[:200]}")
    out = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not out or out == "NOT_A_JOB":
        return None
    footer = (os.environ.get("JOBS_FOOTER") or DEFAULT_FOOTER).replace("\\n", "\n").strip()
    body = out[: LINKEDIN_LIMIT - len(footer) - 2].rstrip()  # trim body, never the footer
    return f"{body}\n\n{footer}"


def publish(text: str) -> str:
    token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError("LINKEDIN_ACCESS_TOKEN secret is missing")
    owner = get_member_urn(token)
    resp = create_ugc_post(token, owner, text, None)  # None = text-only
    post_id = resp.get("id") if isinstance(resp, dict) else None
    if not post_id:
        raise RuntimeError(f"LinkedIn returned no post id: {resp}")
    return f"https://www.linkedin.com/feed/update/{post_id}/"


def run(dry_run: bool, skip_backlog: bool = False) -> int:
    token = (os.environ.get("TBOT_KEY") or "").strip()
    if not token:
        logger.error("TBOT_KEY secret is missing")
        return 1
    jobs_chat = str(os.environ.get("JOBS_CHAT_ID") or DEFAULT_JOBS_CHAT_ID).strip()
    notify = os.environ.get("JOBS_REPLY", "false").lower() == "true"
    max_posts = int(os.environ.get("JOBS_MAX_POSTS") or 10)   # per run; the rest wait for the next run
    posted = 0
    tg = Telegram(token)
    state = load_state()

    offset = state["last_update_id"] + 1 if state["last_update_id"] is not None else None
    try:
        updates = tg.get_updates(offset)
    except RuntimeError as exc:
        if "webhook" in str(exc).lower():
            tg.call("deleteWebhook")
            updates = tg.get_updates(offset)
        else:
            raise
    logger.info(f"{len(updates)} new Telegram update(s)")

    failures = 0
    ack_id = state["last_update_id"]  # highest update safe to acknowledge
    blocked = False                    # once a post fails, stop advancing so it is retried next run
    for upd in updates:
        msg = upd.get("channel_post") or {}
        chat_id = str((msg.get("chat") or {}).get("id"))
        msg_id = msg.get("message_id")
        key = f"{chat_id}:{msg_id}"

        def advance():
            nonlocal ack_id
            if not blocked:
                ack_id = upd["update_id"]

        if chat_id != jobs_chat or key in state["processed"]:
            advance()
            continue
        text = extract_text(msg)
        if len(text) < MIN_CHARS or text.startswith("/"):
            advance()
            continue

        if skip_backlog:
            state["processed"][key] = {"posted": now_iso(), "status": "SKIPPED_BACKLOG"}
            advance()
            continue
        if posted >= max_posts:
            blocked = True   # leave the rest for the next run
            continue

        try:
            post = format_job_post(text)
            if post is None:
                logger.info(f"Skipping {key}: not a job post")
                advance()
                continue
            if dry_run:
                logger.info(f"[dry-run] would post {key}:\n{post}\n")
                continue
            url = publish(post)
            posted += 1
            state["processed"][key] = {"posted": now_iso(), "status": "PUBLISHED", "linkedin_url": url}
            logger.info(f"Published {key} -> {url}")
            advance()
            if notify:
                try:
                    tg.reply(chat_id, f"✅ Posted on LinkedIn:\n{url}", msg_id)
                except Exception:
                    pass
        except Exception:
            failures += 1
            blocked = True
            logger.exception(f"Failed on {key} - will retry on the next run")
        if not dry_run:
            state["last_update_id"] = ack_id
            save_state(state)   # save after every post so a crash never double-posts

    if not dry_run:
        state["last_update_id"] = ack_id
        if updates and ack_id is not None:
            tg.get_updates(ack_id + 1)   # acknowledge handled updates
        save_state(state)
    return 1 if failures else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-backlog", action="store_true",
                   help="Mark all waiting Telegram posts as done WITHOUT posting them")
    args = p.parse_args(argv)
    return run(args.dry_run, args.skip_backlog)


if __name__ == "__main__":
    sys.exit(main())
