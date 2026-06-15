import math
from copy import deepcopy
from typing import Any


GEOMETRY_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}


def is_valid_feature_collection(data: dict) -> bool:
    """
    Validate basic GeoJSON FeatureCollection structure.
    """
    if not isinstance(data, dict) or data.get("type") != "FeatureCollection":
        return False
    features = data.get("features")
    if not isinstance(features, list):
        return False
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            return False
        if "geometry" not in feature:
            return False
        if feature.get("geometry") is not None and not isinstance(feature.get("geometry"), dict):
            return False
        if not isinstance(feature.get("properties", {}), dict):
            return False
    return True


def normalize_geojson(data: dict) -> dict:
    """
    Accept Feature, FeatureCollection, Geometry, or list of features.
    Return a clean FeatureCollection.
    """
    if isinstance(data, list):
        return {
            "type": "FeatureCollection",
            "features": [_as_feature(item) for item in data if _as_feature(item) is not None],
        }

    if not isinstance(data, dict):
        return {"type": "FeatureCollection", "features": []}

    geojson_type = data.get("type")
    if geojson_type == "FeatureCollection":
        features = data.get("features") if isinstance(data.get("features"), list) else []
        return {
            "type": "FeatureCollection",
            "features": [_as_feature(feature) for feature in features if _as_feature(feature) is not None],
        }
    if geojson_type == "Feature":
        feature = _as_feature(data)
        return {"type": "FeatureCollection", "features": [feature] if feature else []}
    if geojson_type in GEOMETRY_TYPES:
        return {
            "type": "FeatureCollection",
            "features": [{"type": "Feature", "properties": {}, "geometry": deepcopy(data)}],
        }
    if isinstance(data.get("geometry"), dict):
        feature = _as_feature(data)
        return {"type": "FeatureCollection", "features": [feature] if feature else []}
    return {"type": "FeatureCollection", "features": []}


def validate_polygon_coordinates(geometry: dict) -> tuple[bool, list[str]]:
    """
    Validate Polygon/MultiPolygon coordinate structure.
    Check lon/lat shape, closed rings, minimum points, numeric values.
    """
    errors: list[str] = []
    if not isinstance(geometry, dict):
        return False, ["geometry must be an object"]

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        _validate_polygon(coordinates, "polygon", errors)
    elif geometry_type == "MultiPolygon":
        if not isinstance(coordinates, list) or not coordinates:
            errors.append("multipolygon coordinates must be a non-empty list")
        else:
            for index, polygon in enumerate(coordinates):
                _validate_polygon(polygon, f"multipolygon[{index}]", errors)
    else:
        errors.append(f"unsupported polygon geometry type: {geometry_type}")

    return not errors, errors


def calculate_bbox_from_geojson(feature_collection: dict) -> list[float] | None:
    """
    Return [minLng, minLat, maxLng, maxLat].
    """
    normalized = normalize_geojson(feature_collection)
    coordinates: list[tuple[float, float]] = []
    for feature in normalized.get("features", []):
        coordinates.extend(_geometry_coordinates(feature.get("geometry")))
    if not coordinates:
        return None

    lngs = [point[0] for point in coordinates]
    lats = [point[1] for point in coordinates]
    return [min(lngs), min(lats), max(lngs), max(lats)]


def make_circular_buffer_geojson(
    center_lng: float,
    center_lat: float,
    radius_km: float,
    *,
    points: int = 64,
    properties: dict | None = None,
) -> dict:
    """
    Create a GeoJSON Feature representing a circular buffer polygon.
    Approximate using lon/lat degree conversion:
    latitude: 1 degree ~= 111 km
    longitude: 1 degree ~= 111 km x cos(latitude)
    """
    center_lng = float(center_lng)
    center_lat = float(center_lat)
    radius_km = float(radius_km)
    points = max(8, int(points))
    latitude_degrees = radius_km / 111.0
    cos_lat = max(abs(math.cos(math.radians(center_lat))), 0.01)
    longitude_degrees = radius_km / (111.0 * cos_lat)

    ring = []
    for index in range(points):
        angle = (2 * math.pi * index) / points
        ring.append(
            [
                round(center_lng + longitude_degrees * math.cos(angle), 6),
                round(center_lat + latitude_degrees * math.sin(angle), 6),
            ]
        )
    ring.append(ring[0])

    props = dict(properties or {})
    props.setdefault("buffer_type", "circular_buffer")
    props.setdefault("radius_km", radius_km)

    return {
        "type": "Feature",
        "properties": props,
        "geometry": {"type": "Polygon", "coordinates": [ring]},
    }


def validate_report_geometries(report: dict) -> dict:
    """
    Validate:
    - boundaries.merged_polygon
    - boundaries.region_boundary
    - analysis.zones
    - hazard.risk_polygons if present
    - routes if present

    Return:
    {
      "valid": true/false,
      "warnings": [],
      "errors": [],
      "geometry_counts": {},
      "bbox": []
    }
    """
    warnings: list[str] = []
    errors: list[str] = []
    counts: dict[str, int] = {}
    bbox_sources: list[dict] = []

    boundaries = report.get("boundaries", {}) if isinstance(report, dict) else {}
    merged = normalize_geojson(boundaries.get("merged_polygon", {}))
    counts["boundaries.merged_polygon"] = len(merged.get("features", []))
    if counts["boundaries.merged_polygon"] == 0:
        warnings.append("boundaries.merged_polygon is missing or empty")
    else:
        _validate_polygon_feature_collection(merged, "boundaries.merged_polygon", errors)
        bbox_sources.append(merged)

    region = normalize_geojson(boundaries.get("region_boundary", {}))
    counts["boundaries.region_boundary"] = len(region.get("features", []))
    if counts["boundaries.region_boundary"]:
        _validate_feature_collection(region, "boundaries.region_boundary", errors)
        bbox_sources.append(region)

    zones = normalize_geojson(report.get("analysis", {}).get("zones", {}) if isinstance(report, dict) else {})
    counts["analysis.zones"] = len(zones.get("features", []))
    if counts["analysis.zones"] == 0:
        warnings.append("analysis.zones is missing or empty")
    else:
        _validate_polygon_feature_collection(zones, "analysis.zones", errors)
        bbox_sources.append(zones)

    risk_polygons = report.get("hazard", {}).get("risk_polygons") if isinstance(report, dict) else None
    if risk_polygons is not None:
        risk_fc = normalize_geojson(risk_polygons)
        counts["hazard.risk_polygons"] = len(risk_fc.get("features", []))
        _validate_polygon_feature_collection(risk_fc, "hazard.risk_polygons", errors)
        bbox_sources.append(risk_fc)
    else:
        counts["hazard.risk_polygons"] = 0

    routes = report.get("routes", {}) if isinstance(report, dict) else {}
    for key, value in routes.items():
        route_fc = normalize_geojson(value)
        counts[f"routes.{key}"] = len(route_fc.get("features", []))
        if route_fc.get("features"):
            _validate_feature_collection(route_fc, f"routes.{key}", errors)
            _validate_line_features(route_fc, f"routes.{key}", errors)
            bbox_sources.append(route_fc)

    bbox = _combined_bbox(bbox_sources)
    if bbox is None:
        warnings.append("no usable coordinates found for bbox calculation")

    return {
        "valid": not errors,
        "warnings": warnings,
        "errors": errors,
        "geometry_counts": counts,
        "bbox": bbox or [],
    }


def explain_shapefile_handling() -> dict:
    """
    Return a note explaining:
    - shapefile consists of .shp, .shx, .dbf
    - frontend needs GeoJSON
    - shapefile should be zipped and converted server-side
    - R2/frontend should use zones.geojson
    """
    return {
        "frontend_format": "GeoJSON FeatureCollection",
        "shapefile_components": [".shp", ".shx", ".dbf"],
        "handling": (
            "Raw shapefiles should be zipped and converted server-side before they reach MapLibre. "
            "The frontend and R2 artifacts should consume zones.geojson, boundaries.geojson, and routes.geojson."
        ),
        "circular_buffers": "Represent circular buffers as GeoJSON Polygon features with radius metadata in properties.",
        "conversion_dependency": "No shapefile conversion dependency is required by this report-agent test suite.",
    }


def _as_feature(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") == "Feature":
        return {
            "type": "Feature",
            "properties": deepcopy(item.get("properties")) if isinstance(item.get("properties"), dict) else {},
            "geometry": deepcopy(item.get("geometry")) if item.get("geometry") is not None else None,
        }
    if item.get("type") in GEOMETRY_TYPES:
        return {"type": "Feature", "properties": {}, "geometry": deepcopy(item)}
    if isinstance(item.get("geometry"), dict):
        return {
            "type": "Feature",
            "properties": deepcopy(item.get("properties")) if isinstance(item.get("properties"), dict) else {},
            "geometry": deepcopy(item.get("geometry")),
        }
    return None


def _validate_feature_collection(feature_collection: dict, label: str, errors: list[str]) -> None:
    if not is_valid_feature_collection(feature_collection):
        errors.append(f"{label} is not a valid FeatureCollection")


def _validate_polygon_feature_collection(feature_collection: dict, label: str, errors: list[str]) -> None:
    _validate_feature_collection(feature_collection, label, errors)
    for index, feature in enumerate(feature_collection.get("features", [])):
        geometry = feature.get("geometry")
        valid, polygon_errors = validate_polygon_coordinates(geometry)
        if not valid:
            errors.extend(f"{label}.features[{index}]: {error}" for error in polygon_errors)


def _validate_line_features(feature_collection: dict, label: str, errors: list[str]) -> None:
    for index, feature in enumerate(feature_collection.get("features", [])):
        geometry = feature.get("geometry") or {}
        geometry_type = geometry.get("type")
        if geometry_type not in {"LineString", "MultiLineString"}:
            errors.append(f"{label}.features[{index}]: expected LineString or MultiLineString, got {geometry_type}")
            continue
        if len(_geometry_coordinates(geometry)) < 2:
            errors.append(f"{label}.features[{index}]: route geometry needs at least 2 coordinates")


def _validate_polygon(coordinates: Any, label: str, errors: list[str]) -> None:
    if not isinstance(coordinates, list) or not coordinates:
        errors.append(f"{label} coordinates must be a non-empty list of rings")
        return
    for ring_index, ring in enumerate(coordinates):
        _validate_ring(ring, f"{label}.ring[{ring_index}]", errors)


def _validate_ring(ring: Any, label: str, errors: list[str]) -> None:
    if not isinstance(ring, list):
        errors.append(f"{label} must be a list")
        return
    if len(ring) < 4:
        errors.append(f"{label} must contain at least 4 coordinate positions")
        return

    parsed = []
    for index, coordinate in enumerate(ring):
        point = _parse_position(coordinate)
        if point is None:
            errors.append(f"{label}[{index}] must be a numeric [lng, lat] position")
        else:
            lng, lat = point
            if not -180 <= lng <= 180:
                errors.append(f"{label}[{index}] longitude out of range: {lng}")
            if not -90 <= lat <= 90:
                errors.append(f"{label}[{index}] latitude out of range: {lat}")
            parsed.append(point)

    if len(parsed) == len(ring) and not _same_position(parsed[0], parsed[-1]):
        errors.append(f"{label} must be closed: first and last coordinates differ")


def _parse_position(value: Any) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    try:
        lng = float(value[0])
        lat = float(value[1])
    except (TypeError, ValueError):
        return None
    if math.isnan(lng) or math.isnan(lat) or math.isinf(lng) or math.isinf(lat):
        return None
    return lng, lat


def _same_position(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return abs(first[0] - second[0]) <= 1e-9 and abs(first[1] - second[1]) <= 1e-9


def _geometry_coordinates(geometry: dict | None) -> list[tuple[float, float]]:
    if not isinstance(geometry, dict):
        return []
    geometry_type = geometry.get("type")
    if geometry_type == "GeometryCollection":
        coordinates = []
        for item in geometry.get("geometries", []):
            coordinates.extend(_geometry_coordinates(item))
        return coordinates
    return _positions_from_coordinates(geometry.get("coordinates"))


def _positions_from_coordinates(value: Any) -> list[tuple[float, float]]:
    point = _parse_position(value)
    if point is not None:
        return [point]
    if isinstance(value, list):
        coordinates = []
        for item in value:
            coordinates.extend(_positions_from_coordinates(item))
        return coordinates
    return []


def _combined_bbox(feature_collections: list[dict]) -> list[float] | None:
    bboxes = [bbox for bbox in (calculate_bbox_from_geojson(item) for item in feature_collections) if bbox]
    if not bboxes:
        return None
    return [
        min(bbox[0] for bbox in bboxes),
        min(bbox[1] for bbox in bboxes),
        max(bbox[2] for bbox in bboxes),
        max(bbox[3] for bbox in bboxes),
    ]
