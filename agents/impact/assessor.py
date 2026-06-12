async def assess_impact(hazard_data: dict):
    return {
        "population_affected": 0,
        "hospitals_at_risk": 0,
        "roads_blocked_km": 0.0,
        "schools_affected": 0,
        "vulnerability_score": 0.0,
    }
