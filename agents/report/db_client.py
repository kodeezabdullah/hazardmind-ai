import json
import os
from pathlib import Path

import asyncpg
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent


async def ensure_final_reports_table() -> None:
    """
    Create the final_reports table if it does not exist.
    """
    conn = await _connect()
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS final_reports (
                event_id TEXT PRIMARY KEY,
                location TEXT,
                hazard_type TEXT,
                overall_severity TEXT,
                summary TEXT,
                detailed_body TEXT,
                pdf_url TEXT,
                map_url TEXT,
                model_sources JSONB,
                report_json JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        await conn.execute(
            """
            ALTER TABLE final_reports
                ADD COLUMN IF NOT EXISTS event_id TEXT,
                ADD COLUMN IF NOT EXISTS location TEXT,
                ADD COLUMN IF NOT EXISTS hazard_type TEXT,
                ADD COLUMN IF NOT EXISTS overall_severity TEXT,
                ADD COLUMN IF NOT EXISTS summary TEXT,
                ADD COLUMN IF NOT EXISTS detailed_body TEXT,
                ADD COLUMN IF NOT EXISTS pdf_url TEXT,
                ADD COLUMN IF NOT EXISTS map_url TEXT,
                ADD COLUMN IF NOT EXISTS model_sources JSONB,
                ADD COLUMN IF NOT EXISTS report_json JSONB,
                ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW(),
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
            """
        )
    except Exception as exc:
        raise RuntimeError(f"Neon final_reports table setup failed: {type(exc).__name__}") from None
    finally:
        await conn.close()


async def write_final_report_metadata(report: dict) -> None:
    """
    Upsert final report metadata using event_id.
    """
    report_section = report.get("report", {})
    conn = await _connect()
    try:
        event_id_text = str(report.get("event_id", ""))
        event_id_is_uuid = await _event_id_column_is_uuid(conn)
        lookup_event_id = event_id_text
        stored_event_id = None if event_id_is_uuid else event_id_text
        where_clause = "report_json ->> 'event_id' = $1" if event_id_is_uuid else "event_id = $1"
        result = await conn.execute(
            f"""
            UPDATE final_reports SET
                location = $2,
                hazard_type = $3,
                overall_severity = $4,
                summary = $5,
                detailed_body = $6,
                pdf_url = $7,
                map_url = $8,
                model_sources = $9::jsonb,
                report_json = $10::jsonb,
                updated_at = NOW()
            WHERE {where_clause};
            """,
            lookup_event_id,
            report.get("location"),
            report.get("hazard_type"),
            report.get("overall_severity"),
            report_section.get("summary"),
            report_section.get("detailed_body"),
            report_section.get("pdf_url"),
            report_section.get("map_url"),
            json.dumps(report.get("model_sources", {})),
            json.dumps(report),
        )
        if result == "UPDATE 0":
            await conn.execute(
                """
                INSERT INTO final_reports (
                    event_id,
                    location,
                    hazard_type,
                    overall_severity,
                    summary,
                    detailed_body,
                    pdf_url,
                    map_url,
                    model_sources,
                    report_json,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb, $10::jsonb, NOW());
                """,
                stored_event_id,
                report.get("location"),
                report.get("hazard_type"),
                report.get("overall_severity"),
                report_section.get("summary"),
                report_section.get("detailed_body"),
                report_section.get("pdf_url"),
                report_section.get("map_url"),
                json.dumps(report.get("model_sources", {})),
                json.dumps(report),
            )
    except Exception as exc:
        raise RuntimeError(f"Neon final_reports upsert failed: {type(exc).__name__}") from None
    finally:
        await conn.close()


def _database_url() -> str:
    load_dotenv(BASE_DIR / ".env")
    database_url = os.getenv("NEON_DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing required Neon environment variable: NEON_DATABASE_URL")
    return database_url


async def _connect():
    try:
        return await asyncpg.connect(_database_url())
    except Exception as exc:
        raise RuntimeError(f"Neon connection failed: {type(exc).__name__}") from None


async def _event_id_column_is_uuid(conn) -> bool:
    row = await conn.fetchrow(
        """
        SELECT udt_name
        FROM information_schema.columns
        WHERE table_name = 'final_reports'
          AND column_name = 'event_id';
        """
    )
    return bool(row and row["udt_name"] == "uuid")
