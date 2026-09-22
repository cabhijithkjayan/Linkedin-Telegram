"""
LinkedIn publishing helper.
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Optional

import requests

AUTH_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
ME_URL = "https://api.linkedin.com/v2/me"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"
ASSETS_URL = "https://api.linkedin.com/v2/assets?action=registerUpload"
UGC_POSTS_URL = "https://api.linkedin.com/v2/ugcPosts"


def get_authorization_url(client_id: str, redirect_uri: str, state: str = "state123", scopes: str = "openid profile email w_member_social") -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "scope": scopes,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


def exchange_code_for_access_token(code: str, redirect_uri: str, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> Optional[dict]:
    client_id = client_id or os.environ.get("LINKEDIN_CLIENT_ID")
    client_secret = client_secret or os.environ.get("LINKEDIN_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError("LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be provided")
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "client_secret": client_secret,
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_member_urn(access_token: str) -> Optional[str]:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    errors = []
    try:
        resp = requests.get(USERINFO_URL, headers=headers, timeout=30)
        if resp.status_code == 200:
            j = resp.json()
            member_id = j.get("sub") or j.get("id")
            if member_id:
                return f"urn:li:person:{member_id}"
            errors.append("/userinfo returned no member id")
        else:
            errors.append(f"/userinfo HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        errors.append(f"/userinfo request error: {exc}")

    try:
        resp = requests.get(ME_URL, headers=headers, timeout=30)
        if resp.status_code == 200:
            j = resp.json()
            member_id = j.get("id")
            if member_id:
                return f"urn:li:person:{member_id}"
            errors.append("/me returned no member id")
        else:
            errors.append(f"/me HTTP {resp.status_code}: {resp.text[:300]}")
    except Exception as exc:
        errors.append(f"/me request error: {exc}")

    raise RuntimeError("LinkedIn member lookup failed: " + " | ".join(errors))


def register_image_upload(access_token: str, owner_urn: str) -> Optional[dict]:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    body = {
        "registerUploadRequest": {
            "owner": owner_urn,
            "recipes": ["urn:li:digitalmediaRecipe:feedshare-image"],
            "serviceRelationships": [
                {"identifier": "urn:li:userGeneratedContent", "relationshipType": "OWNER"}
            ],
        }
    }
    resp = requests.post(ASSETS_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()


def upload_image_to_url(upload_url: str, image_path: str) -> None:
    headers = {"Content-Type": "application/octet-stream"}
    with open(image_path, "rb") as f:
        resp = requests.put(upload_url, data=f, headers=headers, timeout=60)
    resp.raise_for_status()


def create_ugc_post(access_token: str, author_urn: str, post_text: str, asset_urn: Optional[str] = None, visibility: str = "PUBLIC") -> dict:
    headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
    media = []
    share_media_category = "NONE"
    if asset_urn:
        share_media_category = "IMAGE"
        media = [
            {
                "status": "READY",
                "description": {"text": ""},
                "media": asset_urn,
                "title": {"text": ""},
            }
        ]

    body = {
        "author": author_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": post_text},
                "shareMediaCategory": share_media_category,
                "media": media,
            }
        },
        "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility},
    }
    resp = requests.post(UGC_POSTS_URL, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    try:
        result = resp.json()
    except ValueError:
        result = {}
    if isinstance(result, dict) and not result.get("id"):
        response_id = resp.headers.get("x-restli-id")
        if response_id:
            result["id"] = response_id
    return result


if __name__ == "__main__":
    print("LinkedIn helper module. Use from the orchestrator or import functions to run OAuth and publishing flows.")
