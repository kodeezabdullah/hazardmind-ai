# HazardMind Report Agent

Zohair's Report Agent generates the final executive report JSON, static risk map, and PDF artifact for the HazardMind AI demo pipeline.

## Hardcore Testing

Quick local validation:

```powershell
.\agents\report\venv\Scripts\python.exe agents/report/hardcore_test.py --quick
```

Full local validation with LLM checks:

```powershell
.\agents\report\venv\Scripts\python.exe agents/report/hardcore_test.py --include-llm
```

Optional R2 validation:

```powershell
.\agents\report\venv\Scripts\python.exe agents/report/hardcore_test.py --quick --include-r2
```

Band-message fixture validation:

```powershell
.\agents\report\venv\Scripts\python.exe agents/report/agent.py --band-message-file agents/report/test_fixtures/report_trigger_message.txt --emit-band-response
```

The quick test validates:

- Band trigger parsing and completion-message JSON shape
- required environment variables as present/missing booleans only
- GeoJSON fixture validity
- circular buffer polygon validity
- malformed geometry failure handling
- report geometry compatibility
- static map generation for normal, circular, multipolygon, large, and no-zone cases
- PDF generation with intelligence and model-source sections
- DB safety for non-UUID demo IDs

## Band Contract

The Report Agent is contract-ready for Abdullah's backend/Band SDK trigger. The live SDK runner is not included here; backend orchestration should call:

```python
from agents.report.pipeline import run_report_pipeline

result = await run_report_pipeline(
    event_id="backend-generated-uuid",
    fetch_from_db=True,
    upload_r2=True,
    write_db=True,
    incoming_payload=parsed_payload,
)
```

Incoming Band messages may contain natural text followed by trailing JSON. Use `agents/report/band_contract.py` to parse the message and build the completion response.

Expected completion data:

```json
{
  "event_id": "same uuid",
  "agent": "hazardmind-report",
  "status": "complete",
  "step": "report",
  "data": {
    "pdf_url": "https://public-r2-url/events/uuid/report.pdf",
    "map_url": "https://hazardmind.vercel.app/map/uuid",
    "executive_summary": "Full text...",
    "confidence_level": "HIGH",
    "recommended_response_level": "NDMA Level-3"
  }
}
```

`map_url` is the frontend route, not the local static PNG and not the R2 risk-map object. The static PNG is still generated locally for PDF rendering. `pdf_url` points to `events/{event_id}/report.pdf` when R2 upload is enabled.

Run the frontend build separately:

```powershell
cd frontend
npm run build
```

## Geometry Contract

The frontend uses MapLibre and expects GeoJSON, not raw shapefiles. Previous agents should send zones, boundaries, and routes as GeoJSON FeatureCollections in WGS84 longitude/latitude order.

Satellite outputs may provide `geojson_url`; hazard outputs may provide `hazard_zones_geojson` as either a URL or a GeoJSON object; PostGIS geometries should be converted to GeoJSON before map overlay. Raw shapefiles must not be sent directly to the frontend.

Expected `zones.geojson` format:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "properties": {
        "zone_id": "zone-001",
        "severity": "critical",
        "risk_level": "HIGH",
        "hazard_type": "flood",
        "area_km2": 12.4
      },
      "geometry": {
        "type": "Polygon",
        "coordinates": []
      }
    }
  ]
}
```

Circular buffers should be encoded as Polygon features:

```json
{
  "type": "Feature",
  "properties": {
    "buffer_type": "hospital_impact_radius",
    "radius_km": 2.5,
    "facility_name": "Hospital Name"
  },
  "geometry": {
    "type": "Polygon",
    "coordinates": []
  }
}
```

## Shapefile Handling

A shapefile normally consists of `.shp`, `.shx`, and `.dbf` sidecar files. Raw shapefiles are not suitable for direct frontend overlay. If an upstream agent produces shapefiles, zip the shapefile bundle and convert it server-side before the frontend or R2 layer consumes it.

Preferred artifact flow:

```text
upstream geometry
-> zones.geojson / boundaries.geojson / routes.geojson
-> Report Agent validation
-> frontend MapLibre overlay
```

Use `agents/report/geometry_utils.py` to normalize and validate Feature, FeatureCollection, Geometry, or feature-list inputs before wiring live Band/backend messages into the Report Agent.
