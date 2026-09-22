import os

from linkedin_automation.linkedin_service import (
    create_ugc_post,
    get_member_urn,
    register_image_upload,
    upload_image_to_url,
)


def main() -> None:
    access_token = os.environ["LINKEDIN_ACCESS_TOKEN"]
    owner_urn = get_member_urn(access_token)
    print("Owner URN:", owner_urn)

    reg = register_image_upload(access_token, owner_urn)
    upload_url = reg.get("value", {}).get("uploadMechanism", {}).get("com.linkedin.digitalmedia.uploading.MediaUploadHttpRequest", {}).get("uploadUrl")
    asset_urn = reg.get("value", {}).get("asset")

    if not upload_url or not asset_urn:
        raise RuntimeError(f"Invalid registration response: {reg}")

    upload_image_to_url(upload_url, "generated_images/post.png")
    resp = create_ugc_post(access_token, owner_urn, "Sample LinkedIn post text here.", asset_urn)
    print(resp)


if __name__ == "__main__":
    main()
