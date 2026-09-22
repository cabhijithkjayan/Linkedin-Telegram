"""
Anthropic Claude integration wrapper.
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

try:
    from anthropic import Client as AnthropicClient
    _HAS_ANTHROPIC = True
except Exception:
    _HAS_ANTHROPIC = False
    import requests


def load_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_prompt(template: str, idea: str, audience: str = "business professionals in spice trading", tone: str = "professional, practical, human-sounding", cta: str = "To know more and download resources visit:\nhttps://abhijithkjayan.bolt.host/") -> str:
    prompt = template.replace("{idea}", idea)
    prompt = prompt.replace("[PASTE MY RAW INPUT HERE]", idea)
    prompt = prompt.replace("{audience}", audience)
    prompt = prompt.replace("{tone}", tone)
    prompt = prompt.replace("{cta}", cta)
    return prompt


def clean_post_text(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", lambda match: _unicode_bold(match.group(1)), text)
    text = re.sub(r"__(.+?)__", lambda match: _unicode_bold(match.group(1)), text)
    text = re.sub(r"(```?|(?<!\*)\*(?!\*))", "", text)
    text = re.sub(r"^\s*(?:[-*+]\s+|[•→✓➜]\s*)", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+\n", "\n", text)
    lines = text.strip().splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and (index == 0 or (stripped.endswith(":") and len(stripped) <= 60)):
            lines[index] = _unicode_bold(stripped)
    return "\n".join(lines).strip()


def _unicode_bold(text: str) -> str:
    bold_upper = "𝐀𝐁𝐂𝐃𝐄𝐅𝐆𝐇𝐈𝐉𝐊𝐋𝐌𝐍𝐎𝐏𝐐𝐑𝐒𝐓𝐔𝐕𝐖𝐗𝐘𝐙"
    bold_lower = "𝐚𝐛𝐜𝐝𝐞𝐟𝐠𝐡𝐢𝐣𝐤𝐥𝐦𝐧𝐨𝐩𝐪𝐫𝐬𝐭𝐮𝐯𝐰𝐱𝐲𝐳"
    bold_digits = "𝟎𝟏𝟐𝟑𝟒𝟓𝟔𝟕𝟖𝟗"
    result = []
    for character in text:
        if "A" <= character <= "Z":
            result.append(bold_upper[ord(character) - ord("A")])
        elif "a" <= character <= "z":
            result.append(bold_lower[ord(character) - ord("a")])
        elif "0" <= character <= "9":
            result.append(bold_digits[ord(character) - ord("0")])
        else:
            result.append(character)
    return "".join(result)


def generate_post(idea: str, prompt_file: str = "prompts/claude_linkedin_prompt.txt", model: str = "claude-haiku-4-5-20251001", max_tokens: int = 800, temperature: float = 0.2) -> Optional[str]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY not set in environment. Cannot call Claude.")
        return None

    template = load_template(prompt_file)
    prompt = build_prompt(template, idea)

    def supports_temperature(model_name: str) -> bool:
        name = (model_name or "").lower()
        legacy_names = ("claude-2", "claude-instant")
        if "claude-2" in name or "claude-instant" in name:
            return True
        return False

    request_kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if supports_temperature(model):
        request_kwargs["temperature"] = temperature

    if _HAS_ANTHROPIC:
        client = AnthropicClient(api_key=api_key)
        try:
            if supports_temperature(model):
                resp = client.messages.create(**request_kwargs, temperature=temperature)
            else:
                resp = client.messages.create(**request_kwargs)
            text = None
            if hasattr(resp, "content"):
                for block in getattr(resp, "content", []) or []:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        break
                    if hasattr(block, "text"):
                        text = block.text
                        break
            if not text:
                text = getattr(resp, "output_text", None) or getattr(resp, "completion", None)
            if isinstance(text, str):
                cleaned = clean_post_text(text)
                return cleaned if cleaned else None
            return None
        except Exception as e:
            print("Anthropic client error:", e, file=sys.stderr)
            return None

    url = "https://api.anthropic.com/v1/messages"
    anthropic_version = os.environ.get("ANTHROPIC_VERSION", "2023-06-01")
    headers = {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": anthropic_version,
    }
    candidate_models = []
    env_model = os.environ.get("ANTHROPIC_MODEL")
    if env_model:
        candidate_models.append(env_model)
    candidate_models.extend([
        model,
        "claude-haiku-4-5-20251001",
        "claude-sonnet-5",
        "claude-sonnet-5-20250929",
        "claude-sonnet-4",
        "claude-sonnet-4-20250514",
        "claude-opus-4",
        "claude-opus-4-20250514",
        "claude-3-5-sonnet-latest",
        "claude-3-5-haiku-latest",
        "claude-3-opus-latest",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
        "claude-3-7-sonnet-20250219",
        "claude-2.1",
        "claude-2.0",
        "claude-instant-1.2",
    ])
    seen = set()
    candidate_models = [m for m in candidate_models if not (m in seen or seen.add(m))]

    for model_name in candidate_models:
        body = {
            "model": model_name,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if supports_temperature(model_name):
            body["temperature"] = temperature
        try:
            r = requests.post(url, headers=headers, json=body, timeout=60)
            if r.status_code == 404:
                print(f"Anthropic model {model_name} not available (404): {r.text}", file=sys.stderr)
                continue
            if r.status_code in (401, 403):
                print(f"Anthropic auth failed for model {model_name} (status {r.status_code}): {r.text}", file=sys.stderr)
                continue
            r.raise_for_status()
            j = r.json()
            text = None
            if isinstance(j, dict):
                content = j.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text" and block.get("text"):
                                text = block.get("text")
                                break
                if not text:
                    try:
                        text = j["output_text"]
                    except Exception:
                        pass
                if not text:
                    text = j.get("completion") or j.get("output") or j.get("result")
            if not text:
                text = str(j)
            cleaned = clean_post_text(text) if isinstance(text, str) else None
            return cleaned if cleaned else None
        except requests.HTTPError as e:
            try:
                content = r.text
            except Exception:
                content = "<no response body>"
            print(f"Anthropic HTTP error for model {model_name}: {e}; response: {content}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"Anthropic request error for model {model_name}: {e}", file=sys.stderr)
            continue

    print("Anthropic generation failed across all candidate models.", file=sys.stderr)
    return None


if __name__ == "__main__":
    print("This module provides generate_post(); run via the orchestrator main.py")
