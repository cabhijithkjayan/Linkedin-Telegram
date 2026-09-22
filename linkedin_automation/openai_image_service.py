"""
OpenAI image generation service.
"""
from __future__ import annotations

import argparse
import base64
import os
import sys
from typing import Optional

import requests

try:
    import openai
except Exception:
    print("Missing dependency: openai. Install with: pip install -r requirements.txt")
    raise


def load_template(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def build_prompt(template: str, post_text: str) -> str:
    if "{post_text}" in template:
        return template.replace("{post_text}", post_text)
    return template + "\n\nPost text:\n" + post_text


def _extract_b64_image(payload):
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                if first.get("b64_json"):
                    return first["b64_json"]
                if first.get("url"):
                    response = requests.get(first["url"], timeout=60)
                    response.raise_for_status()
                    return base64.b64encode(response.content).decode("utf-8")
    else:
        try:
            data = getattr(payload, "data", None)
            if data:
                first = data[0]
                if hasattr(first, "b64_json") and first.b64_json:
                    return first.b64_json
                if hasattr(first, "url") and first.url:
                    response = requests.get(first.url, timeout=60)
                    response.raise_for_status()
                    return base64.b64encode(response.content).decode("utf-8")
        except Exception:
            pass
    return None


def generate_image(prompt: str, output_path: str, size: str = "auto") -> Optional[str]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY not set in environment. Aborting.")
        return None

    try:
        if hasattr(openai, "OpenAI"):
            client = openai.OpenAI(api_key=api_key)
            result = client.images.generate(model="gpt-image-1", prompt=prompt, size=size)
            b64 = _extract_b64_image(result)
        else:
            openai.api_key = api_key
            result = openai.Image.create(prompt=prompt, n=1, size=size, response_format="b64_json")
            b64 = _extract_b64_image(result)
    except Exception as e:
        print("OpenAI Image API error:", e, file=sys.stderr)
        return None

    if not b64:
        print("OpenAI Image API returned no image data.", file=sys.stderr)
        return None

    image_bytes = base64.b64decode(b64)
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(image_bytes)
    return output_path


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--post-text", help="Final LinkedIn post text to base the image on", required=True)
    parser.add_argument("--prompt-file", help="Path to image prompt template", default="prompts/image_prompt.txt")
    parser.add_argument("--output", help="Output image path", default="generated_images/post_image.png")
    parser.add_argument("--size", help="Image size (e.g. 1024x1024, 1536x1024, auto)", default="auto")
    args = parser.parse_args(argv)

    template = load_template(args.prompt_file)
    prompt = build_prompt(template, args.post_text)
    out = generate_image(prompt, args.output, size=args.size)
    if not out:
        return 1
    print(f"Image generated: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
