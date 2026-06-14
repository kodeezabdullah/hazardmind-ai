import os
from pathlib import Path

import boto3
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
REQUIRED_R2_ENV_VARS = (
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_R2_KEY",
    "CLOUDFLARE_R2_SECRET",
    "CLOUDFLARE_R2_BUCKET",
    "CLOUDFLARE_R2_PUBLIC_URL",
)


def upload_file_to_r2(local_path: str, object_key: str, content_type: str) -> str:
    """
    Uploads a local file to Cloudflare R2 and returns its public URL.
    """
    load_dotenv(BASE_DIR / ".env")
    _validate_r2_environment()

    path = Path(local_path)
    if not path.exists():
        raise FileNotFoundError(f"R2 upload source file does not exist: {path}")

    normalized_key = _normalize_object_key(object_key)
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["CLOUDFLARE_R2_KEY"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET"],
        region_name="auto",
    )

    try:
        client.upload_file(
            str(path),
            os.environ["CLOUDFLARE_R2_BUCKET"],
            normalized_key,
            ExtraArgs={"ContentType": content_type},
        )
    except Exception as exc:
        raise RuntimeError(f"R2 upload failed for object '{normalized_key}': {type(exc).__name__}") from None

    return _public_url_for(normalized_key)


def _validate_r2_environment() -> None:
    missing = [name for name in REQUIRED_R2_ENV_VARS if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required R2 environment variables: {', '.join(missing)}")


def _normalize_object_key(object_key: str) -> str:
    return "/".join(part for part in object_key.replace("\\", "/").split("/") if part)


def _public_url_for(object_key: str) -> str:
    base_url = os.environ["CLOUDFLARE_R2_PUBLIC_URL"].rstrip("/")
    return f"{base_url}/{_normalize_object_key(object_key)}"
