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

The quick test validates:

- required environment variables as present/missing booleans only
- GeoJSON fixture validity
- circular buffer polygon validity
- malformed geometry failure handling
- report geometry compatibility
- static map generation for normal, circular, multipolygon, large, and no-zone cases
- PDF generation with intelligence and model-source sections
- DB safety for non-UUID demo IDs

Run the frontend build separately:

```powershell
cd frontend
npm run build
```

## Geometry Contract

The frontend uses MapLibre and expects GeoJSON, not raw shapefiles. Previous agents should send zones, boundaries, and routes as GeoJSON FeatureCollections in WGS84 longitude/latitude order.

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
