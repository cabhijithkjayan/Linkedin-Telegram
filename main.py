"""
Orchestrator for the LinkedIn automation DRY_RUN pipeline.

Usage examples:
  python main.py --url "https://drive.google.com/uc?export=download&id=..." --dry-run
"""
from __future__ import annotations

import argparse
import csv
import datetime
import io
import os

from linkedin_automation import mark_processing
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

logger = setup_logger()


def checkpoint(stage: str, status: str, details: str = "") -> None:
    os.makedirs("outputs", exist_ok=True)
    line = f"- **{stage}**: `{status}`{f' - {details}' if details else ''}\n"
    report_path = os.path.join("outputs", "checkpoints.md")
    with open(report_path, "a", encoding="utf-8") as report:
        report.write(line)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path and os.path.abspath(summary_path) != os.path.abspath(report_path):
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(line)


def load_local_dotenv(path: str = ".env") -> None:
    try:
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and (key not in os.environ or not os.environ.get(key)):
                    os.environ[key] = value
    except Exception:
        pass


load_local_dotenv()


def _row_idea(selected: dict) -> str:
    return (
        selected.get("Idea")
        or selected.get("Idea ")
        or selected.get("LinkedIn Post Topic")
        or selected.get("LinkedIn Post topic")
        or selected.get("topic")
        or selected.get("Topic")
        or ""
    ).strip()


def parse_csv_text(text: str):
    text = text.lstrip("\ufeff")
    f = io.StringIO(text)
    reader = csv.DictReader(f)
    rows = list(reader)
    return reader.fieldnames or [], rows


def write_csv(path: str, rows, fieldnames):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def linkedin_post_url(post_id: str) -> str:
    return f"https://www.linkedin.com/feed/update/{post_id}/"


def _row_key(row, serial_col):
    serial = (row.get(serial_col) or "").strip()
    if serial:
        return f"serial:{serial}"
    idea = _row_idea(row).casefold()
    return f"idea:{idea}" if idea else ""


def merge_tracking_rows(fresh_rows, fresh_fieldnames, local_rows, local_fieldnames):
    """Refresh ideas from the public CSV while retaining local run metadata."""
    fresh_serial_col, _ = mark_processing.detect_header(fresh_fieldnames)
    local_serial_col, _ = mark_processing.detect_header(local_fieldnames)
    local_by_key = {
        key: row
        for row in local_rows
        if (key := _row_key(row, local_serial_col))
    }
    fieldnames = list(fresh_fieldnames)
    for fieldname in local_fieldnames:
        if fieldname not in fieldnames:
            fieldnames.append(fieldname)
    fieldnames = mark_processing.ensure_columns(fieldnames)

    fresh_keys = {_row_key(row, fresh_serial_col) for row in fresh_rows}
    merged_rows = []
    matched_keys = set()
    for fresh_row in fresh_rows:
        key = _row_key(fresh_row, fresh_serial_col)
        local_row = local_by_key.get(key)
        merged_row = dict(fresh_row)
        if local_row:
            matched_keys.add(key)
            for column_name in mark_processing.RECOMMENDED_COLUMNS:
                if column_name in local_row:
                    merged_row[column_name] = local_row[column_name]
        merged_rows.append(merged_row)

    for key, local_row in local_by_key.items():
        if key not in matched_keys and key not in fresh_keys:
            merged_rows.append(dict(local_row))

    return merged_rows, fieldnames


def run(url: str, dry_run: bool = True, output_csv: str = "Ideas_marked.csv") -> int:
    checkpoint("Workspace and configuration", "PASS", f"cwd={os.getcwd()}")
    logger.info("Downloading fresh CSV from public URL")
    try:
        text = mark_processing.download_csv(url)
    except Exception as exc:
        checkpoint("Fresh public CSV download", "FAIL", str(exc))
        raise
    fresh_fieldnames, fresh_rows = parse_csv_text(text)
    if not fresh_fieldnames or not fresh_rows:
        checkpoint("Fresh public CSV download", "FAIL", "CSV is empty or has no header")
        logger.error("Public CSV is empty or has no header")
        return 2
    checkpoint("Fresh public CSV download", "PASS", f"rows={len(fresh_rows)}")

    local_rows = []
    local_fieldnames = []
    if os.path.exists(output_csv):
        try:
            with open(output_csv, "r", newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                local_fieldnames = reader.fieldnames or []
                local_rows = list(reader)
        except (OSError, csv.Error) as exc:
            logger.warning(f"Could not read local tracking CSV; starting from fresh data: {exc}")

    if local_rows and local_fieldnames:
        rows, fieldnames = merge_tracking_rows(
            fresh_rows, fresh_fieldnames, local_rows, local_fieldnames
        )
        logger.info("Merged fresh public ideas with local tracking metadata")
    else:
        rows = fresh_rows
        fieldnames = mark_processing.ensure_columns(fresh_fieldnames)

    serial_col, _ = mark_processing.detect_header(fieldnames)
    idx = mark_processing.find_next_unused(rows, serial_col)
    if idx == -1:
        idea_columns = [
            name for name in fresh_fieldnames
            if name.strip().lower() in {
                "idea", "idea ", "linkedin post topic", "linkedin post topic",
                "topic", "title", "post topic"
            }
        ]
        checkpoint(
            "Idea selection",
            "FAIL",
            f"no usable unused ideas; rows={len(rows)}; idea_columns={idea_columns or 'none'}",
        )
        logger.info("No unused ideas found in fresh CSV.")
        write_csv(output_csv, rows, fieldnames)
        return 0

    selected = rows[idx]
    idea_text = _row_idea(selected)
    if not idea_text:
        checkpoint("Idea selection", "FAIL", "selected idea is blank")
        logger.error("Fresh CSV selected a blank idea; refusing to generate an empty post")
        return 2
    serial_val = selected.get(serial_col, "").strip() or "(unknown)"
    mark_processing.mark_row_processing(rows, idx, fieldnames)
    write_csv(output_csv, rows, fieldnames)
    logger.info(f"Marked Serial {serial_val} as PROCESSING and wrote {output_csv}")
    checkpoint("Idea selection", "PASS", f"serial={serial_val}")

    logger.info("Generating LinkedIn post via Claude (Anthropic)")
    post_text = generate_post(idea_text)
    if not post_text:
        checkpoint("Claude post generation", "FAIL")
        logger.error("Claude generation failed; leaving row as PROCESSING for retry")
        rows[idx]["Error"] = "Claude generation failed"
        write_csv(output_csv, rows, fieldnames)
        return 2
    post_text = clean_post_text(post_text)
    checkpoint("Claude post generation", "PASS", f"characters={len(post_text)}")

    os.makedirs("outputs", exist_ok=True)
    post_file = os.path.join("outputs", f"post_{serial_val}.txt")
    with open(post_file, "w", encoding="utf-8") as f:
        f.write(post_text)
    logger.info(f"Saved generated post to {post_file}")

    logger.info("Building image prompt and calling OpenAI Images API")
    template = load_image_template("prompts/image_prompt.txt")
    image_prompt = build_image_prompt(template, post_text)
    img_out = os.path.join("generated_images", f"post_{serial_val}.png")
    img_path = generate_image(image_prompt, img_out)
    if not img_path:
        checkpoint("OpenAI image generation", "FAIL")
        logger.error("Image generation failed; leaving row as PROCESSING for retry")
        rows[idx]["Error"] = "Image generation failed"
        write_csv(output_csv, rows, fieldnames)
        return 3

    logger.info(f"Image generated: {img_path}")
    checkpoint("OpenAI image generation", "PASS", f"file={img_path}")
    rows[idx]["Post Content"] = post_text
    rows[idx]["Image File"] = img_path

    if dry_run:
        checkpoint("LinkedIn publishing", "SKIP", "dry_run=true")
        rows[idx]["Status"] = "READY"
        write_csv(output_csv, rows, fieldnames)
        logger.info(f"Updated CSV written to {output_csv} (dry_run={dry_run})")
        logger.info("DRY_RUN complete. Review outputs and CSV before enabling real publishing.")
        return 0

    try:
        checkpoint("LinkedIn publishing", "START")
        if rows[idx].get("LinkedIn Post ID"):
            logger.warning("Selected row already has a LinkedIn Post ID; skipping publish to avoid duplicate.")
            rows[idx]["Status"] = "READY"
            write_csv(output_csv, rows, fieldnames)
            return 0

        access_token = os.environ.get("LINKEDIN_ACCESS_TOKEN")
        if not access_token:
            logger.error("LINKEDIN_ACCESS_TOKEN not set in environment. Aborting publish.")
            rows[idx]["Error"] = "Missing LINKEDIN_ACCESS_TOKEN"
            rows[idx]["Status"] = "FAILED"
            write_csv(output_csv, rows, fieldnames)
            return 4
        access_token = access_token.strip()
        if access_token.lower().startswith("bearer "):
            access_token = access_token[7:].strip()
        if any(character.isspace() for character in access_token):
            raise ValueError("LINKEDIN_ACCESS_TOKEN contains whitespace; save it as one clean line")

        try:
            owner_urn = get_member_urn(access_token)
        except Exception as exc:
            checkpoint("LinkedIn member lookup", "FAIL", str(exc))
            logger.error(str(exc))
            rows[idx]["Error"] = str(exc)
            rows[idx]["Status"] = "FAILED"
            write_csv(output_csv, rows, fieldnames)
            return 5
        if not owner_urn:
            checkpoint("LinkedIn member lookup", "FAIL")
            logger.error("Failed to resolve member URN. Aborting publish.")
            rows[idx]["Error"] = "Failed to resolve member URN"
            rows[idx]["Status"] = "FAILED"
            write_csv(output_csv, rows, fieldnames)
            return 5
        checkpoint("LinkedIn member lookup", "PASS")

        logger.info(f"Registering image upload for owner {owner_urn}")
        reg = register_image_upload(access_token, owner_urn)
        upload_url = reg.get("value", {}).get("uploadMechanism", {}).get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}).get("uploadUrl")
        asset_urn = reg.get("value", {}).get("asset")
        if not upload_url or not asset_urn:
            checkpoint("LinkedIn image registration", "FAIL")
            logger.error("Image registerUpload response missing upload URL or asset URN")
            rows[idx]["Error"] = f"Bad registerUpload response: {reg}"
            rows[idx]["Status"] = "FAILED"
            write_csv(output_csv, rows, fieldnames)
            return 6
        checkpoint("LinkedIn image registration", "PASS")

        logger.info("Uploading image to LinkedIn upload URL")
        upload_image_to_url(upload_url, img_path)
        checkpoint("LinkedIn image upload", "PASS")

        logger.info("Creating UGC post on LinkedIn")
        resp = create_ugc_post(access_token, owner_urn, post_text, asset_urn)
        post_id = resp.get("id") if isinstance(resp, dict) else None
        if not post_id:
            raise RuntimeError(f"LinkedIn returned no post ID: {resp}")

        post_url = linkedin_post_url(post_id)
        url_file = os.path.join("outputs", f"post_{serial_val}_url.txt")
        with open(url_file, "w", encoding="utf-8") as f:
            f.write(post_url)

        now = datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        rows[idx]["Status"] = "PUBLISHED"
        rows[idx]["Published Date"] = now
        rows[idx]["LinkedIn Post ID"] = post_id
        rows[idx]["LinkedIn URL"] = post_url
        write_csv(output_csv, rows, fieldnames)
        logger.info(f"PUBLISHED LINKEDIN URL: {post_url}")
        logger.info(f"Published post; CSV updated with Post ID {post_id}")
        checkpoint("LinkedIn post creation", "PASS", post_url)
    except Exception as e:
        checkpoint("LinkedIn publishing", "FAIL", str(e))
        logger.exception("Exception during LinkedIn publish")
        rows[idx]["Error"] = str(e)
        rows[idx]["Status"] = "FAILED"
        write_csv(output_csv, rows, fieldnames)
        return 7

    logger.info("Publish complete.")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True, help="Public Google Drive download URL")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Run in DRY_RUN mode (default: true)")
    parser.add_argument("--no-dry-run", dest="dry_run", action="store_false", help="Disable DRY_RUN and attempt real publishing")
    parser.add_argument("--output-csv", default="Ideas_marked.csv")
    args = parser.parse_args(argv)
    return run(args.url, dry_run=args.dry_run, output_csv=args.output_csv)


if __name__ == "__main__":
    raise SystemExit(main())
