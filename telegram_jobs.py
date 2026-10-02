"""
Telegram JOBS channel -> LinkedIn (text only, in the fixed job-post template).

Every run:
  1. Reads new posts from the Jobs Telegram channel with the bot (TBOT_KEY).
  2. Fills the fixed job-post template from the message with plain code
     (NO AI, NO API keys, NO images) - only facts found in the post, links kept as-is.
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
import logging
import os
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("telegram_jobs")

LI_API = "https://api.linkedin.com/v2"

DEFAULT_JOBS_CHAT_ID = "-1004296140869"
STATE_FILE = "telegram_jobs_state.json"
MIN_CHARS = 15
LINKEDIN_LIMIT_HARD = 2950  # LinkedIn hard limit is 3000

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


DEFAULT_FOOTER = (
    "To know more and download resources visit:\n"
    "https://abhijithkjayan.bolt.host/"
)

CV_LINE = "Upload your CV in my dashboard and join the community"
UAE_CITIES = ["dubai", "abu dhabi", "sharjah", "ajman", "fujairah", "ras al khaimah", "umm al quwain", "al ain"]
# lines that are internal notes for the channel owner - never posted
IGNORE_RE = re.compile(r"ats match|best cv|track\s*[a-z]\b|apply link checked|cv match|match score|^\s*[🟢🧭]", re.I)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
TITLE_LABEL_RE = re.compile(r"^(?:job\s*title|position|role|vacancy)\s*[:\-]\s*(.*)$", re.I)
HEADER_RE = re.compile(r"job alert|new jobs?|jobs? (?:post|update|opening)|hiring now|vacanc|naukri|indeed|linkedin|bayt|gulftalent|alert", re.I)
EDU_RE = re.compile(r"education|qualification|degree|bachelor|master|\bmba\b|diploma|graduate|\bcpa\b|\bacca\b|\bcma\b|chartered|\bca\b", re.I)
LEADING_JUNK = re.compile(r"^[^\w#]+")
FUNCTION_TAGS = [  # (keywords in title, hashtags)
    (("financ", "fp&a", "account", "audit", "tax", "treasury", "controller"), ["Finance", "Accounting"]),
    (("software", "developer", "engineer", "devops", "data", "it ", "cloud", "cyber"), ["Technology", "Engineering"]),
    (("sales", "business development", "account manager"), ["Sales", "BusinessDevelopment"]),
    (("market", "brand", "content", "seo", "social media"), ["Marketing"]),
    (("hr ", "human resource", "recruit", "talent"), ["HR", "Recruitment"]),
    (("supply chain", "logistic", "procurement", "warehouse"), ["SupplyChain", "Logistics"]),
    (("nurse", "doctor", "medical", "pharma", "health"), ["Healthcare"]),
    (("project manager", "operations", "admin"), ["Operations", "ProjectManagement"]),
    (("design", "ux", "ui "), ["Design"]),
    (("legal", "compliance", "risk"), ["Legal", "Compliance"]),
]


def _clean(line: str) -> str:
    return LEADING_JUNK.sub("", line).strip()


def _after_colon(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else _clean(line)


def parse_job(raw: str) -> dict:
    lines = [l.strip() for l in raw.splitlines()]
    lines = [l for l in lines if l]
    keep = [l for l in lines if not IGNORE_RE.search(l)]
    title = ""
    for l in keep:                                   # explicit "Job Title: ..." / "Role: ..." wins
        m = TITLE_LABEL_RE.match(_clean(l))
        if m and m.group(1).strip():
            title = m.group(1).strip()
            break
    if not title:                                    # else first line that is not a header like "Naukrigulf job alert"
        for l in keep:
            c = _clean(l)
            if c and not HEADER_RE.search(c) and not URL_RE.search(c):
                title = c
                break
    keep = [l for l in keep if _clean(l) != title]
    job = {"title": title, "company": "", "location": "", "type": "", "experience": "", "education": "",
           "skills": [], "highlights": [], "apply": [], "email": "", "other": []}

    section = None
    used_urls = set()
    for line in keep:
        low = line.lower()
        body = _clean(line)

        if low.startswith("🏢") or low.startswith("company:") or low.startswith("recruiter:"):
            job["company"] = _after_colon(line) if ":" in line else body
            section = None
        elif low.startswith("📍") or low.startswith("location:"):
            job["location"] = _after_colon(line) if ":" in line else body
            section = None
        elif low.startswith("💼") or "employment type" in low or "job type" in low:
            job["type"] = _after_colon(line) if ":" in line else body
            section = None
        elif EDU_RE.search(low) and len(body) < 160 and not URL_RE.search(line) and not job["education"]:
            job["education"] = _after_colon(line) if ":" in line else body
            section = None
        elif "experience" in low and len(body) < 120 and not URL_RE.search(line):
            job["experience"] = _after_colon(line) if ":" in line else body
            section = None
        elif line.startswith("✅") or low.startswith("skills"):
            txt = _after_colon(line) if ":" in line else body
            job["skills"] += [x.strip() for x in re.split(r"[,;|·•/]", txt) if x.strip()]
            section = None
        elif re.match(r"^\W*apply( links?| here| now| via)?\b", low) or low.startswith("📩"):
            section = "apply"
            for m in URL_RE.finditer(line):
                job["apply"].append(("", m.group(0)))
                used_urls.add(m.group(0))
        elif re.search(r"responsibilit|requirement|role highlights|duties|what you", low):
            section = "highlights"
        elif "email" in low and "subject" not in low:
            m = EMAIL_RE.search(line)
            if m:
                job["email"] = m.group(0)
            section = "email"
        elif low.startswith("subject"):
            pass
        elif low.startswith("company info") or low.startswith("🌐") or "website" in low:
            section = "other"
            for m in URL_RE.finditer(line):
                job["other"].append(m.group(0))
                used_urls.add(m.group(0))
        else:
            m_url = URL_RE.search(line)
            m_mail = EMAIL_RE.search(line)
            if section == "apply" and m_url:
                name = _clean(line[: m_url.start()]).rstrip("(:- ").strip()
                job["apply"].append((name, m_url.group(0)))
                used_urls.add(m_url.group(0))
            elif section == "other" and m_url:
                job["other"].append(m_url.group(0)); used_urls.add(m_url.group(0))
            elif section == "email" and m_mail:
                job["email"] = job["email"] or m_mail.group(0)
            elif section == "highlights" and line[:1] in "•-*·–▪":
                job["highlights"].append(body)
            elif m_url and m_url.group(0) not in used_urls:
                name = _clean(line[: m_url.start()]).rstrip("(:- ").strip()
                job["apply"].append((name, m_url.group(0))); used_urls.add(m_url.group(0))
            elif m_mail and not job["email"]:
                job["email"] = m_mail.group(0)

    if not job["email"]:
        m = EMAIL_RE.search(raw)
        job["email"] = m.group(0) if m else ""
    # de-duplicate
    seen, apply = set(), []
    for name, url in job["apply"]:
        if url not in seen:
            seen.add(url); apply.append((name, url))
    job["apply"] = apply
    job["other"] = list(dict.fromkeys(job["other"]))
    job["skills"] = list(dict.fromkeys(job["skills"]))[:5]
    job["highlights"] = job["highlights"][:4]
    return job


def _nice_location(loc: str) -> str:
    l = loc.strip()
    if l and any(c in l.lower() for c in UAE_CITIES) and "uae" not in l.lower() and "emirates" not in l.lower():
        return f"{l}, UAE"
    return l


def _hashtags(title: str, location: str) -> str:
    t = f" {title.lower()} "
    tags = []
    for keys, tg in FUNCTION_TAGS:
        if any(k in t for k in keys):
            tags = tg
            break
    if not tags:
        words = [re.sub(r"\W", "", w).capitalize() for w in title.split() if len(w) > 3]
        tags = words[:1]
    in_uae = (not location) or any(c in location.lower() for c in UAE_CITIES) or "uae" in location.lower()
    if in_uae:
        base = ["Hiring", "JobAlert", "UAEJobs", "DubaiJobs", "AbuDhabiJobs"]
    else:
        city = re.sub(r"\W", "", location.split(",")[0].title())
        base = ["Hiring", "JobAlert"] + ([f"{city}Jobs"] if city else [])
    tail = ["CareerOpportunity", "FinanceJobs"] if "Finance" in tags else ["CareerOpportunity"]
    allt = list(dict.fromkeys(base + tags + tail))
    return " ".join("#" + x for x in allt)


def format_job_post(raw: str) -> str | None:
    """Fill the fixed template from the channel post - no AI, no API keys. None = skip."""
    job = parse_job(raw)
    if len(job["title"]) < 3:
        return None
    loc = _nice_location(job["location"])
    out = [f"🚨 JOB OPPORTUNITY | {job['title']}"]
    if loc:
        out.append(f"📍 Location: {loc}")
    if job["company"]:
        out.append(f"🏢 Company / Recruiter: {job['company']}")
    if job["type"]:
        out.append(f"💼 Employment Type: {job['type']}")
    if job["experience"]:
        out.append(f"🎯 Experience: {job['experience']}")
    if job["skills"]:
        out.append("A new opportunity is available for professionals with experience in:")
        out += [f"🔹 {s}" for s in job["skills"]]
    else:
        out.append("A new opportunity is available for professionals in this field.")
    if job["highlights"]:
        out += ["", "🎯 Role Highlights"] + [f"• {h}" for h in job["highlights"]]
    exp, edu = job["experience"].strip().rstrip("."), job["education"].strip().rstrip(".")
    if exp or edu:
        parts = []
        if exp:
            parts.append(f"{exp} of relevant experience" if re.search(r"\d", exp) and "experience" not in exp.lower() else exp)
        if edu:
            e = edu[0].lower() + edu[1:] if edu[:2].islower() or not edu[:2].isupper() else edu
            art = "an" if e[:1].lower() in "aeiou" else "a"
            parts.append(f"{art} {e}" if re.search(r"degree|bachelor|master|diploma|mba|graduate|certif", e, re.I)
                         else f"an educational background in {e}")
        focus = ", ".join(job["skills"][:4])
        sent = "This role may be relevant for professionals with " + " and ".join(parts)
        sent += f", particularly those experienced in {focus}." if focus else "."
        out += ["", "📄 Candidate Profile", sent]
    if job["apply"]:
        out += ["", "📩 Application:"]
        for name, url in job["apply"]:
            out.append(f"{name}: {url}" if name else url)
    if job["email"]:
        out += ["" if not job["apply"] else "", f"📧 Email: {job['email']}",
                f"Subject: Application – {job['title']}"]
    out += ["", "💡 If you know someone who fits this opportunity, feel free to tag them or share this post.",
            "🔄 Sharing opportunities can help someone in your network discover their next career move.",
            "", _hashtags(job["title"], job["location"]), ""]
    footer = (os.environ.get("JOBS_FOOTER") or DEFAULT_FOOTER).replace("\\n", "\n").strip()
    out.append(footer)
    out.append("")
    out.append(CV_LINE)
    out += job["other"]
    return "\n".join(out)[:LINKEDIN_LIMIT_HARD]


def get_member_urn(token: str) -> str:
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{LI_API}/userinfo", headers=h, timeout=30)
    if r.status_code == 200 and r.json().get("sub"):
        return f"urn:li:person:{r.json()['sub']}"
    r = requests.get(f"{LI_API}/me", headers=h, timeout=30)
    if r.status_code == 200 and r.json().get("id"):
        return f"urn:li:person:{r.json()['id']}"
    raise RuntimeError(f"Could not read LinkedIn profile (token expired?): HTTP {r.status_code} {r.text[:200]}")


def publish(text: str) -> str:
    token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError("LINKEDIN_ACCESS_TOKEN secret is missing")
    owner = get_member_urn(token)
    body = {
        "author": owner,
        "lifecycleState": "PUBLISHED",
        "specificContent": {"com.linkedin.ugc.ShareContent": {
            "shareCommentary": {"text": text},
            "shareMediaCategory": "NONE"}},
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"},
    }
    r = requests.post(f"{LI_API}/ugcPosts", json=body, timeout=60, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0"})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"LinkedIn post failed: HTTP {r.status_code} {r.text[:300]}")
    post_id = r.headers.get("x-restli-id") or (r.json().get("id") if r.text else None)
    if not post_id:
        raise RuntimeError(f"LinkedIn returned no post id: {r.text[:200]}")
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
