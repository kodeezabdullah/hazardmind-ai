"""Cloudflare R2 upload for the satellite agent.

After `processor.process_satellite_imagery` writes a `satellite.png`, this
module pushes it to a Cloudflare R2 bucket (S3-compatible) and returns a public
URL the frontend can load.

R2 is reached through boto3's S3 client pointed at the account's R2 endpoint:
    https://<account_id>.r2.cloudflarestorage.com

Objects are stored under a per-event prefix:
    events/<event_id>/satellite.png

For the demo, three events (peshawar, dhaka, kathmandu) are pre-cached in the
bucket. `check_demo_cache` lets the agent short-circuit the whole download/clip/
export pipeline when one of those is requested.

Credentials come from the environment (loaded from `.env`):
    CLOUDFLARE_R2_KEY / CLOUDFLARE_R2_ACCESS_KEY   - R2 access key id
    CLOUDFLARE_R2_SECRET                            - R2 secret access key
    CLOUDFLARE_R2_BUCKET                            - bucket name
    CLOUDFLARE_ACCOUNT_ID                           - account id (builds endpoint)
    CLOUDFLARE_R2_ENDPOINT                          - optional explicit endpoint
    CLOUDFLARE_R2_PUBLIC_URL                         - optional public base URL
                                                      (e.g. an r2.dev or custom
                                                      domain). Falls back to the
                                                      account r2.dev domain.

Every function logs and returns None on failure rather than raising, matching
the rest of the satellite agent.

Run this file directly for a small smoke test:
    python r2_upload.py
"""

import json
import logging
import os
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Object key prefix; the event id is interpolated to namespace each event.
OBJECT_KEY_TEMPLATE = "events/{event_id}/satellite.png"

# Per-event keys for the full result set produced by the pipeline.
RESULT_KEYS = {
    "true_color": "events/{event_id}/true_color.png",
    "index_map": "events/{event_id}/index_map.png",
    "classification": "events/{event_id}/classification.png",
    "geojson": "events/{event_id}/zones.geojson",
}

# Demo events whose imagery is pre-cached in R2. Requesting one of these can
# skip the live download/clip/export pipeline entirely.
DEMO_EVENTS = ("peshawar", "dhaka", "kathmandu")


def _r2_endpoint() -> Optional[str]:
    """Resolve the R2 S3 endpoint from env vars.

    Prefers an explicit `CLOUDFLARE_R2_ENDPOINT`; otherwise builds it from
    `CLOUDFLARE_ACCOUNT_ID`. Returns None if neither is available.
    """
    endpoint = os.getenv("CLOUDFLARE_R2_ENDPOINT")
    if endpoint:
        return endpoint.rstrip("/")

    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    if account_id:
        return f"https://{account_id}.r2.cloudflarestorage.com"

    return None


def _public_base_url() -> Optional[str]:
    """Resolve the public base URL used to build object URLs.

    Prefers an explicit `CLOUDFLARE_R2_PUBLIC_URL` (an r2.dev domain or a custom
    domain bound to the bucket); otherwise falls back to the account's r2.dev
    domain. Returns None if neither can be determined.
    """
    public = os.getenv("CLOUDFLARE_R2_PUBLIC_URL")
    if public:
        return public.rstrip("/")

    account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID")
    if account_id:
        return f"https://pub-{account_id}.r2.dev"

    return None


def _public_url(key: str) -> Optional[str]:
    """Build the public URL for an object key, or None if unconfigured."""
    base = _public_base_url()
    if not base:
        logger.warning(
            "No CLOUDFLARE_R2_PUBLIC_URL / CLOUDFLARE_ACCOUNT_ID set; "
            "cannot build a public URL for %s",
            key,
        )
        return None
    return f"{base}/{key}"


def get_r2_client():
    """Create a boto3 S3 client configured for Cloudflare R2.

    Reads the endpoint and credentials from the environment. Access key id is
    taken from `CLOUDFLARE_R2_KEY` or `CLOUDFLARE_R2_ACCESS_KEY`; the secret from
    `CLOUDFLARE_R2_SECRET`. Returns the client, or None if configuration is
    missing or the client cannot be built.
    """
    endpoint = _r2_endpoint()
    if not endpoint:
        logger.error(
            "Neither CLOUDFLARE_R2_ENDPOINT nor CLOUDFLARE_ACCOUNT_ID is set; "
            "cannot create R2 client"
        )
        return None

    access_key = os.getenv("CLOUDFLARE_R2_KEY") or os.getenv(
        "CLOUDFLARE_R2_ACCESS_KEY"
    )
    secret_key = os.getenv("CLOUDFLARE_R2_SECRET")

    if not access_key or not secret_key:
        logger.error(
            "CLOUDFLARE_R2_KEY / CLOUDFLARE_R2_SECRET not set; "
            "cannot create R2 client"
        )
        return None

    try:
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            # R2 ignores the region but the SDK requires one; "auto" is the
            # value Cloudflare documents for the S3 API.
            region_name="auto",
            config=Config(signature_version="s3v4"),
        )
    except (BotoCoreError, ValueError) as exc:
        logger.error("Failed to create R2 client: %s", exc)
        return None

    logger.info("Created R2 client for endpoint %s", endpoint)
    return client


def _put_file(client, bucket: str, path: str, key: str, content_type: str) -> Optional[str]:
    """Upload a local file to R2 under `key`; return its public URL or None."""
    if not path or not os.path.exists(path):
        logger.error("File %r does not exist; nothing to upload", path)
        return None
    logger.info("Uploading %s to r2://%s/%s", path, bucket, key)
    try:
        client.upload_file(
            path,
            bucket,
            key,
            ExtraArgs={"ContentType": content_type, "ACL": "public-read"},
        )
    except (BotoCoreError, ClientError, OSError) as exc:
        logger.error("Failed to upload %s to R2: %s", path, exc)
        return None
    return _public_url(key)


def _put_bytes(client, bucket: str, body: bytes, key: str, content_type: str) -> Optional[str]:
    """Upload an in-memory object to R2 under `key`; return its public URL."""
    logger.info("Uploading %d bytes to r2://%s/%s", len(body), bucket, key)
    try:
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            ACL="public-read",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("Failed to upload %s to R2: %s", key, exc)
        return None
    return _public_url(key)


def upload_to_r2(png_path: str, event_id: str) -> Optional[str]:
    """Upload a single satellite PNG to R2 and return its public URL.

    Stores the object at `events/<event_id>/satellite.png` with a public-read
    ACL and an image/png content type. Returns the public URL, or None on
    failure.
    """
    bucket = os.getenv("CLOUDFLARE_R2_BUCKET")
    if not bucket:
        logger.error("CLOUDFLARE_R2_BUCKET not set; cannot upload")
        return None

    client = get_r2_client()
    if client is None:
        return None

    key = OBJECT_KEY_TEMPLATE.format(event_id=event_id)
    url = _put_file(client, bucket, png_path, key, "image/png")
    if url:
        logger.info("Uploaded satellite imagery for %s -> %s", event_id, url)
    return url


def upload_all_results(event_id: str, files_dict: dict) -> dict:
    """Upload the full result set (3 PNGs + zones GeoJSON) for an event.

    Args:
        event_id: namespaces the object keys under events/<event_id>/.
        files_dict: {
            "true_color": <png path>,
            "index_map": <png path>,
            "classification": <png path>,
            "geojson": <GeoJSON dict OR path to a .geojson file>,
        }

    Returns the public URLs (None for any item that could not be uploaded):
        {
            "true_color_url", "index_url", "classification_url", "geojson_url",
        }
    """
    result = {
        "true_color_url": None,
        "index_url": None,
        "classification_url": None,
        "geojson_url": None,
    }

    bucket = os.getenv("CLOUDFLARE_R2_BUCKET")
    if not bucket:
        logger.error("CLOUDFLARE_R2_BUCKET not set; cannot upload results")
        return result

    client = get_r2_client()
    if client is None:
        return result

    def key_for(name: str) -> str:
        return RESULT_KEYS[name].format(event_id=event_id)

    result["true_color_url"] = _put_file(
        client, bucket, files_dict.get("true_color"),
        key_for("true_color"), "image/png",
    )
    result["index_url"] = _put_file(
        client, bucket, files_dict.get("index_map"),
        key_for("index_map"), "image/png",
    )
    result["classification_url"] = _put_file(
        client, bucket, files_dict.get("classification"),
        key_for("classification"), "image/png",
    )

    geojson = files_dict.get("geojson")
    if isinstance(geojson, dict):
        body = json.dumps(geojson).encode("utf-8")
        result["geojson_url"] = _put_bytes(
            client, bucket, body, key_for("geojson"), "application/geo+json"
        )
    elif geojson:
        result["geojson_url"] = _put_file(
            client, bucket, geojson, key_for("geojson"), "application/geo+json"
        )

    logger.info("Uploaded result set for %s: %s", event_id, result)
    return result


def check_demo_cache(event_id: str) -> Optional[str]:
    """Return a public URL for a pre-cached demo event, or None.

    Demo events (`peshawar`, `dhaka`, `kathmandu`) may already have a
    `satellite.png` in the bucket. If the object exists, its public URL is
    returned so the caller can skip the live processing pipeline. For any other
    event id, or if the object is absent / unreachable, returns None and the
    caller should run real processing.
    """
    if not event_id:
        return None

    if event_id.strip().lower() not in DEMO_EVENTS:
        return None

    bucket = os.getenv("CLOUDFLARE_R2_BUCKET")
    if not bucket:
        logger.warning("CLOUDFLARE_R2_BUCKET not set; cannot check demo cache")
        return None

    client = get_r2_client()
    if client is None:
        return None

    key = OBJECT_KEY_TEMPLATE.format(event_id=event_id)

    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        # A 404/NoSuchKey just means it isn't cached; anything else is logged
        # but treated the same way (fall back to real processing).
        code = exc.response.get("Error", {}).get("Code")
        if code in ("404", "NoSuchKey", "NotFound"):
            logger.info("No demo cache for %s; will process live", event_id)
        else:
            logger.warning(
                "Could not check demo cache for %s: %s", event_id, exc
            )
        return None
    except BotoCoreError as exc:
        logger.warning("Could not check demo cache for %s: %s", event_id, exc)
        return None

    url = _public_url(key)
    logger.info("Demo cache hit for %s -> %s", event_id, url)
    return url


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Smoke test: confirm a client can be built, then probe the demo cache for
    # each demo event. Needs valid R2 credentials in the environment.
    client = get_r2_client()
    if client is None:
        print("Could not create R2 client; check credentials in .env")
    else:
        print("R2 client created")
        for demo in DEMO_EVENTS:
            url = check_demo_cache(demo)
            print(f"{demo}: {url or 'not cached'}")
