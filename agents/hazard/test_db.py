import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()
engine = create_engine(os.getenv("NEON_DATABASE_URL"))

def test_db_write():
    test_id = "a0000000-0000-0000-0000-000000000001"
    with engine.connect() as conn:
        conn.execute(text("""
            INSERT INTO hazard_zones
              (event_id, risk_level, hazard_type, severity,
               overall_confidence, created_at)
            VALUES
              (CAST(:event_id AS uuid), :risk_level, :hazard_type, :severity,
               :overall_confidence, :created_at)
        """), {
            "event_id": test_id,
            "risk_level": "HIGH",
            "hazard_type": "flood",
            "severity": "HIGH",
            "overall_confidence": 0.91,
            "created_at": datetime.now(timezone.utc)
        })
        conn.commit()
        result = conn.execute(text(
            "SELECT event_id, hazard_type, risk_level, severity FROM hazard_zones WHERE event_id = CAST(:id AS uuid)"
        ), {"id": test_id})
        row = result.fetchone()
        print("DB write confirmed:", row)

test_db_write()
