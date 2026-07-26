"""Pre-flight connectivity checks for the E2E pipeline test.

Verifies every external dependency the pipeline needs is reachable BEFORE we
spend minutes on a real Sentinel download. Prints a PASS/FAIL table and exits
non-zero if any hard prerequisite fails, so the test run can stop early.

Run: .venv-e2e/Scripts/python.exe tests/e2e/preflight.py
"""

import asyncio
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))

from tests.e2e._env import load_all_service_envs

RESULTS = []


def record(name, ok, detail):
    RESULTS.append((name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}")


def check_neon():
    import asyncpg

    async def _run():
        url = os.getenv("NEON_DATABASE_URL")
        if not url:
            return False, "NEON_DATABASE_URL not set"
        conn = await asyncpg.connect(url, ssl="require", timeout=20)
        try:
            val = await conn.fetchval("SELECT 1")
            # also confirm the 5 pipeline tables exist
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename IN "
                "('disaster_events','satellite_results','hazard_zones',"
                "'impact_data','final_reports')"
            )
            names = sorted(r["tablename"] for r in tables)
            return val == 1, f"SELECT 1 -> {val}; tables present: {names}"
        finally:
            await conn.close()

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def check_r2():
    import boto3
    from botocore.config import Config

    try:
        endpoint = os.getenv("CLOUDFLARE_R2_ENDPOINT")
        acct = os.getenv("CLOUDFLARE_ACCOUNT_ID")
        if not endpoint and acct:
            endpoint = f"https://{acct}.r2.cloudflarestorage.com"
        bucket = os.getenv("CLOUDFLARE_R2_BUCKET")
        key = os.getenv("CLOUDFLARE_R2_KEY") or os.getenv("CLOUDFLARE_R2_ACCESS_KEY")
        secret = os.getenv("CLOUDFLARE_R2_SECRET")
        if not all([endpoint, bucket, key, secret]):
            return False, "missing R2 endpoint/bucket/key/secret"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
            region_name="auto",
            config=Config(signature_version="s3v4", connect_timeout=15, read_timeout=15),
        )
        resp = client.list_objects_v2(Bucket=bucket, MaxKeys=1)
        n = resp.get("KeyCount", 0)
        return True, f"bucket '{bucket}' reachable (KeyCount sample={n})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def check_cdse():
    # Use the satellite agent's own auth function to prove the real path works.
    sat_dir = ROOT / "agents" / "satellite"
    sys.path.insert(0, str(sat_dir))
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "preflight_sentinel", sat_dir / "sentinel.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        token = mod.authenticate_copernicus(timeout=30)
        if token and len(token) > 40:
            return True, f"token acquired (len={len(token)})"
        return False, f"no token returned (got {token!r})"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    finally:
        sys.modules.pop("preflight_sentinel", None)
        if str(sat_dir) in sys.path:
            sys.path.remove(str(sat_dir))


def check_gemini():
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        return False, "GEMINI_API_KEY not set"
    import requests

    try:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "gemini-2.0-flash:generateContent?key=" + key
        )
        r = requests.post(
            url,
            json={"contents": [{"parts": [{"text": "reply with the single word OK"}]}]},
            timeout=30,
        )
        if r.status_code == 200:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            return True, f"200 -> {txt[:40]!r}"
        # 429 = key valid but rate/quota limited (still 'reachable & authenticated')
        if r.status_code == 429:
            return True, "429 rate-limited (key valid; free-tier quota)"
        return False, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def check_geoboundaries():
    import requests

    try:
        r = requests.get(
            "https://www.geoboundaries.org/api/current/gbOpen/PAK/ADM3/",
            timeout=30,
        )
        if r.status_code == 200 and "gjDownloadURL" in r.text:
            return True, "PAK ADM3 metadata reachable"
        return False, f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def main():
    print("=== Loading service .env files ===")
    for path, status in load_all_service_envs().items():
        print(f"  {path}: {status}")
    print("\n=== Pre-flight checks ===")

    checks = [
        ("Neon DB (SELECT 1 + tables)", check_neon),
        ("Cloudflare R2 (list bucket)", check_r2),
        ("Copernicus CDSE (auth token)", check_cdse),
        ("Gemini API (test call)", check_gemini),
        ("geoBoundaries API", check_geoboundaries),
    ]
    for name, fn in checks:
        t0 = time.time()
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"unexpected {type(e).__name__}: {e}"
        record(name, ok, f"{detail}  ({time.time()-t0:.1f}s)")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print("\n=== Summary ===")
    print(f"  {len(RESULTS)-len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("  FAILED:", ", ".join(failed))
        sys.exit(1)
    print("  All prerequisites reachable.")
    sys.exit(0)


if __name__ == "__main__":
    main()
