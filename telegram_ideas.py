"""
Telegram "Ideas" channel -> LinkedIn post.

Every run:
  1. Reads new messages from the Ideas Telegram channel (bot token in TBOT_KEY).
  2. For each new idea, drafts a LinkedIn post with Claude (same prompt as main.py)
     and creates a matching image with OpenAI (optional - text-only if it fails).
  3. Posts the draft back into the Ideas channel (as a reply to the idea).
  4. Publishes the post on LinkedIn and replies in the channel with the link.

The bot must be an ADMIN of the Ideas channel (with "Post messages" permission).
Processed message ids are kept in telegram_ideas_state.json so nothing is posted twice.

Usage:
  python telegram_ideas.py            # live: posts to Telegram + LinkedIn
  python telegram_ideas.py --dry-run  # drafts to Telegram only, no LinkedIn
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

import requests

from linkedin_automation.claude_service import clean_post_text, generate_post
from linkedin_automation.linkedin_service import (
    create_ugc_post,
    get_member_urn,
    register_image_upload,
    upload_image_to_url,
)
from linkedin_automation.openai_image_service import (
    build_prompt as build_image_prompt,
    generate_image,
    load_template as load_image_template,
)
from linkedin_automation.utils_logger import setup_logger

logger = setup_logger("telegram_ideas")

DEFAULT_IDEAS_CHAT_ID = "-1003739312796"
STATE_FILE = "telegram_ideas_state.json"
MIN_IDEA_CHARS = 15          # ignore tiny messages like "ok", "test"
TG_TEXT_LIMIT = 4000         # Telegram hard limit is 4096


# ------------------------------------------------------------------ helpers
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    state.setdefault("last_update_id", None)
    state.setdefault("processed", {})   # "chat_id:message_id" -> result
    return state


def save_state(state: dict) -> None:
    # keep the file small: only the latest 300 entries
    items = list(state["processed"].items())[-300:]
    state["processed"] = dict(items)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Telegram:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, files=None, **params):
        r = requests.post(f"{self.base}/{method}", data=params, files=files, timeout=60)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"Telegram {method}: HTTP {r.status_code} {r.text[:200]}")
        if not j.get("ok"):
            raise RuntimeError(f"Telegram {method}: {j.get('description')}")
        return j.get("result")

    def get_updates(self, offset=None):
        params = {"timeout": 0, "allowed_updates": json.dumps(["channel_post", "message"])}
        if offset is not None:
            params["offset"] = offset
        return self.call("getUpdates", **params) or []

    def send_text(self, chat_id, text, reply_to=None):
        chunks = [text[i:i + TG_TEXT_LIMIT] for i in range(0, len(text), TG_TEXT_LIMIT)] or [""]
        first = None
        for chunk in chunks:
            params = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": "true"}
            if reply_to:
                params["reply_to_message_id"] = reply_to
                params["allow_sending_without_reply"] = "true"
            res = self.call("sendMessage", **params)
            first = first or res
        return first

    def send_photo(self, chat_id, path, caption="", reply_to=None):
        params = {"chat_id": chat_id, "caption": caption[:1000]}
        if reply_to:
            params["reply_to_message_id"] = reply_to
            params["allow_sending_without_reply"] = "true"
        with open(path, "rb") as f:
            return self.call("sendPhoto", files={"photo": f}, **params)


def extract_idea(msg: dict) -> str:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    # add hidden links (text links) so Claude sees them as reference
    for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
        if ent.get("url") and ent["url"] not in text:
            text += "\n" + ent["url"]
    return text.strip()


def publish_to_linkedin(post_text: str, img_path: str | None) -> str:
    token = (os.environ.get("LINKEDIN_ACCESS_TOKEN") or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        raise RuntimeError("LINKEDIN_ACCESS_TOKEN secret is missing")
    owner = get_member_urn(token)
    asset = None
    if img_path:
        try:
            reg = register_image_upload(token, owner)
            upload_url = (reg.get("value", {}).get("uploadMechanism", {})
                          .get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {})
                          .get("uploadUrl"))
            asset = reg.get("value", {}).get("asset")
            if upload_url and asset:
                upload_image_to_url(upload_url, img_path)
            else:
                asset = None
        except Exception as exc:
            logger.warning(f"LinkedIn image upload failed, posting text only: {exc}")
            asset = None
    resp = create_ugc_post(token, owner, post_text, asset)
    post_id = resp.get("id") if isinstance(resp, dict) else None
    if not post_id:
        raise RuntimeError(f"LinkedIn returned no post id: {resp}")
    return f"https://www.linkedin.com/feed/update/{post_id}/"


# ------------------------------------------------------------------ main flow
def process_idea(tg: Telegram, chat_id, msg_id, idea: str, dry_run: bool) -> dict:
    result = {"received": now_iso(), "idea": idea[:200]}

    logger.info(f"Drafting LinkedIn post for message {msg_id}")
    post_text = generate_post(idea)
    if not post_text:
        raise RuntimeError("Claude could not draft the post (check ANTHROPIC_API_KEY)")
    post_text = clean_post_text(post_text)

    img_path = None
    if os.environ.get("OPENAI_API_KEY") and os.environ.get("IDEAS_WITH_IMAGE", "true").lower() != "false":
        try:
            template = load_image_template("prompts/image_prompt.txt")
            img_path = generate_image(build_image_prompt(template, post_text),
                                      os.path.join("generated_images", f"idea_{msg_id}.png"))
        except Exception as exc:
            logger.warning(f"Image generation failed, continuing text only: {exc}")
            img_path = None

    # 1) draft back into the Ideas channel
    if img_path:
        try:
            tg.send_photo(chat_id, img_path, caption="🖼 Image for the LinkedIn post below", reply_to=msg_id)
        except Exception as exc:
            logger.warning(f"Could not send image to Telegram: {exc}")
    tg.send_text(chat_id, "📝 LinkedIn draft\n\n" + post_text, reply_to=msg_id)
    result["draft_sent"] = True

    # 2) publish on LinkedIn
    if dry_run:
        tg.send_text(chat_id, "🧪 Dry run - not posted on LinkedIn.", reply_to=msg_id)
        result["status"] = "DRAFT_ONLY"
        return result

    url = publish_to_linkedin(post_text, img_path)
    tg.send_text(chat_id, f"✅ Posted on LinkedIn:\n{url}", reply_to=msg_id)
    result["status"] = "PUBLISHED"
    result["linkedin_url"] = url
    return result


def run(dry_run: bool) -> int:
    token = (os.environ.get("TBOT_KEY") or "").strip()
    if not token:
        logger.error("TBOT_KEY secret is missing")
        return 1
    ideas_chat = str(os.environ.get("IDEAS_CHAT_ID") or DEFAULT_IDEAS_CHAT_ID).strip()
    tg = Telegram(token)
    state = load_state()

    offset = state["last_update_id"] + 1 if state["last_update_id"] is not None else None
    try:
        updates = tg.get_updates(offset)
    except RuntimeError as exc:
        if "webhook" in str(exc).lower():
            logger.warning("Bot had a webhook set - removing it so getUpdates works")
            tg.call("deleteWebhook")
            updates = tg.get_updates(offset)
        else:
            raise
    logger.info(f"{len(updates)} new Telegram update(s)")

    failures = 0
    for upd in updates:
        msg = upd.get("channel_post") or upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id"))
        msg_id = msg.get("message_id")
        key = f"{chat_id}:{msg_id}"

        if chat_id != ideas_chat:
            if chat.get("type") == "channel":
                logger.info(f"Ignoring channel '{chat.get('title')}' ({chat_id}); "
                            f"set IDEAS_CHAT_ID if this is the Ideas channel")
            state["last_update_id"] = upd["update_id"]
            continue
        idea = extract_idea(msg)
        if key in state["processed"] or len(idea) < MIN_IDEA_CHARS or idea.startswith("/"):
            state["last_update_id"] = upd["update_id"]
            continue

        try:
            state["processed"][key] = process_idea(tg, chat_id, msg_id, idea, dry_run)
            logger.info(f"Done: {key} -> {state['processed'][key].get('status')}")
        except Exception as exc:
            failures += 1
            logger.exception(f"Failed on {key}")
            state["processed"][key] = {"received": now_iso(), "idea": idea[:200],
                                       "status": "FAILED", "error": str(exc)[:500]}
            try:
                tg.send_text(chat_id, f"⚠️ Could not create/post the LinkedIn post:\n{str(exc)[:500]}",
                             reply_to=msg_id)
            except Exception:
                pass
        state["last_update_id"] = upd["update_id"]
        save_state(state)   # save after every idea so a crash never double-posts

    if updates:
        tg.get_updates(state["last_update_id"] + 1)   # acknowledge handled updates
    save_state(state)
    return 1 if failures else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="Draft to Telegram only, do not post on LinkedIn")
    args = p.parse_args(argv)
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
