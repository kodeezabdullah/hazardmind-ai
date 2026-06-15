import math
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


CANVAS_SIZE = (1600, 1000)
BACKGROUND = "#050B14"
TEXT = "#E8F4FF"
MUTED = "#8EA4B8"
CYAN = "#22D3EE"
BLUE = "#38BDF8"

SEVERITY_STYLES = {
    "critical": {"fill": (185, 28, 28, 170), "outline": "#FF5B5B", "label": "Critical zone"},
    "high": {"fill": (249, 115, 22, 155), "outline": "#FDBA74", "label": "High zone"},
    "medium": {"fill": (250, 204, 21, 145), "outline": "#FDE68A", "label": "Medium zone"},
    "low": {"fill": (20, 184, 166, 140), "outline": "#5EEAD4", "label": "Low zone"},
}


def generate_static_map(report: dict, output_path: str | Path) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    image = Image.new("RGB", CANVAS_SIZE, BACKGROUND)
    draw = ImageDraw.Draw(image)
    fonts = _fonts()

    map_box = (55, 130, 1105, 890)
    side_box = (1140, 130, 1545, 890)

    _draw_background(draw)
    _draw_title(draw, fonts, report)
    projection = _Projection(report.get("boundaries", {}).get("bbox"), map_box)
    _draw_map_frame(draw, fonts, map_box, projection)
    _draw_risk_zones(image, draw, fonts, report, projection)
    _draw_region_boundary(draw, report, projection)
    _draw_evacuation_routes(draw, fonts, report, projection)
    _draw_facilities(draw, fonts, report, projection)
    _draw_north_arrow(draw, fonts, map_box)
    _draw_scale_bar(draw, fonts, map_box, projection)
    _draw_side_panel(draw, fonts, side_box, report)
    _draw_footer(draw, fonts)

    image.save(path)
    return path


class _Projection:
    def __init__(self, bbox: list[float] | None, map_box: tuple[int, int, int, int]):
        bbox = bbox or [71.40, 33.90, 71.65, 34.10]
        min_lon, min_lat, max_lon, max_lat = bbox
        lon_pad = max((max_lon - min_lon) * 0.08, 0.01)
        lat_pad = max((max_lat - min_lat) * 0.08, 0.01)
        self.min_lon = min_lon - lon_pad
        self.max_lon = max_lon + lon_pad
        self.min_lat = min_lat - lat_pad
        self.max_lat = max_lat + lat_pad
        self.mean_lat = (self.min_lat + self.max_lat) / 2
        self.map_box = map_box

        width_px = map_box[2] - map_box[0]
        height_px = map_box[3] - map_box[1]
        width_km = self._x_km(self.max_lon) - self._x_km(self.min_lon)
        height_km = self._y_km(self.max_lat) - self._y_km(self.min_lat)
        self.scale = min(width_px / width_km, height_px / height_km)
        used_w = width_km * self.scale
        used_h = height_km * self.scale
        self.offset_x = map_box[0] + (width_px - used_w) / 2
        self.offset_y = map_box[1] + (height_px - used_h) / 2
        self.width_km = width_km

    def to_px(self, lon: float, lat: float) -> tuple[int, int]:
        x = self.offset_x + (self._x_km(lon) - self._x_km(self.min_lon)) * self.scale
        y = self.offset_y + (self._y_km(self.max_lat) - self._y_km(lat)) * self.scale
        return round(x), round(y)

    def lon_ticks(self, count: int = 5) -> list[float]:
        return _linspace(self.min_lon, self.max_lon, count)

    def lat_ticks(self, count: int = 5) -> list[float]:
        return _linspace(self.min_lat, self.max_lat, count)

    def km_to_px(self, km: float) -> int:
        return round(km * self.scale)

    def _x_km(self, lon: float) -> float:
        return lon * 111 * math.cos(math.radians(self.mean_lat))

    @staticmethod
    def _y_km(lat: float) -> float:
        return lat * 111


def _fonts() -> dict[str, ImageFont.ImageFont]:
    def load(size: int, bold: bool = False):
        candidates = [
            "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            # Linux
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            # Mac
            "/System/Library/Fonts/Helvetica.ttc",
        ]
        for candidate in candidates:
            if not os.path.exists(candidate):
                continue
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
        return ImageFont.load_default()

    return {
        "title": load(42, True),
        "subtitle": load(22),
        "heading": load(24, True),
        "body": load(17),
        "small": load(14),
        "tiny": load(12),
        "label": load(15, True),
    }


def _draw_background(draw: ImageDraw.ImageDraw) -> None:
    draw.rectangle((0, 0, 1600, 1000), fill=BACKGROUND)
    for x in range(0, 1600, 40):
        fill = "#0B1E31" if x % 160 else "#0E2A44"
        draw.line((x, 0, x, 1000), fill=fill, width=1)
    for y in range(0, 1000, 40):
        fill = "#0B1E31" if y % 160 else "#0E2A44"
        draw.line((0, y, 1600, y), fill=fill, width=1)
    draw.rectangle((0, 0, 1600, 1000), outline="#0E7490", width=2)


def _draw_title(draw: ImageDraw.ImageDraw, fonts: dict, report: dict) -> None:
    draw.rounded_rectangle((35, 25, 1565, 105), radius=12, fill="#06101D", outline="#164E63", width=2)
    draw.text((60, 38), "HazardMind AI - Disaster Risk Map", font=fonts["title"], fill=TEXT)
    subtitle = (
        f"{report.get('location', 'Unknown location')} | {report.get('hazard_type', 'Hazard')} | "
        f"{report.get('overall_severity', 'UNKNOWN')} | Event: {report.get('event_id', 'unknown')}"
    )
    draw.text((62, 83), subtitle, font=fonts["subtitle"], fill="#A5F3FC")
    severity = report.get("overall_severity", "UNKNOWN")
    draw.rounded_rectangle((1320, 42, 1538, 88), radius=8, fill="#2A0D12", outline="#F87171", width=2)
    draw.text((1340, 53), f"SEVERITY: {severity}", font=fonts["label"], fill="#FECACA")


def _draw_map_frame(draw: ImageDraw.ImageDraw, fonts: dict, box: tuple[int, int, int, int], projection: _Projection) -> None:
    draw.rounded_rectangle(box, radius=10, fill="#07111F", outline="#67E8F9", width=2)
    inner = _inset(box, 20)
    draw.rectangle(inner, outline="#0E7490", width=1)

    for lon in projection.lon_ticks():
        x, _ = projection.to_px(lon, projection.min_lat)
        draw.line((x, inner[1], x, inner[3]), fill="#12344E", width=1)
        draw.text((x - 22, inner[3] + 7), f"{lon:.2f}E", font=fonts["tiny"], fill=MUTED)
    for lat in projection.lat_ticks():
        _, y = projection.to_px(projection.min_lon, lat)
        draw.line((inner[0], y, inner[2], y), fill="#12344E", width=1)
        draw.text((inner[0] - 48, y - 7), f"{lat:.2f}N", font=fonts["tiny"], fill=MUTED)

    draw.text((box[0] + 20, box[1] + 16), "MAIN MAP FRAME | WGS84", font=fonts["tiny"], fill="#A5F3FC")


def _draw_region_boundary(draw: ImageDraw.ImageDraw, report: dict, projection: _Projection) -> None:
    region = report.get("boundaries", {}).get("region_boundary", {})
    for feature in region.get("features", []):
        _draw_geojson_geometry(draw, feature.get("geometry"), projection, outline="#155E75", width=1, dash=True)

    boundary = report.get("boundaries", {}).get("merged_polygon", {})
    for width, color in [(9, "#083344"), (5, "#0891B2"), (2, "#A5F3FC")]:
        _draw_geojson_geometry(draw, boundary.get("geometry"), projection, outline=color, width=width)


def _draw_risk_zones(image: Image.Image, draw: ImageDraw.ImageDraw, fonts: dict, report: dict, projection: _Projection) -> None:
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    zones = report.get("analysis", {}).get("zones", {}).get("features", [])
    if not zones:
        _draw_label(draw, (410, 505), "No hazard polygons available", fonts["heading"], fill="#FDE68A", bg="#06101D")
        return

    for feature in zones:
        props = feature.get("properties", {})
        severity = str(props.get("severity", "low")).lower()
        style = SEVERITY_STYLES.get(severity, SEVERITY_STYLES["low"])
        polygons = _geometry_polygons(feature.get("geometry"))
        for polygon in polygons:
            points = [projection.to_px(lon, lat) for lon, lat in polygon]
            if len(points) < 3:
                continue
            overlay_draw.polygon(points, fill=style["fill"], outline=style["outline"])
            draw.line(points + [points[0]], fill=style["outline"], width=3)
            zone_id = props.get("zone_id")
            if zone_id:
                cx = round(sum(p[0] for p in points) / len(points))
                cy = round(sum(p[1] for p in points) / len(points))
                _draw_label(draw, (cx, cy), str(zone_id), fonts["label"])
    composite = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    image.paste(composite)


def _draw_evacuation_routes(draw: ImageDraw.ImageDraw, fonts: dict, report: dict, projection: _Projection) -> None:
    routes = report.get("routes", {}).get("evacuation_routes", {}).get("features", [])
    for feature in routes:
        coords = feature.get("geometry", {}).get("coordinates", [])
        points = [projection.to_px(lon, lat) for lon, lat in coords]
        if len(points) < 2:
            continue
        draw.line(points, fill="#082F49", width=10, joint="curve")
        draw.line(points, fill=BLUE, width=5, joint="curve")
        _draw_route_arrows(draw, points, BLUE)
        name = feature.get("properties", {}).get("name")
        if name:
            mid = points[len(points) // 2]
            _draw_label(draw, (mid[0] + 10, mid[1] - 18), name, fonts["small"], fill="#DBEAFE", bg="#0B2440")


def _draw_facilities(draw: ImageDraw.ImageDraw, fonts: dict, report: dict, projection: _Projection) -> None:
    for facility in report.get("impact", {}).get("critical_facilities", []):
        x, y = projection.to_px(facility.get("lng", 0), facility.get("lat", 0))
        risk = facility.get("risk", "MEDIUM")
        color = "#FF5B5B" if risk == "HIGH" else "#22D3EE"
        draw.ellipse((x - 11, y - 11, x + 11, y + 11), fill="#07111F", outline=color, width=3)
        draw.line((x - 6, y, x + 6, y), fill=color, width=3)
        draw.line((x, y - 6, x, y + 6), fill=color, width=3)
        name = facility.get("name", "Facility")
        _draw_label(draw, (x + 15, y - 8), name, fonts["tiny"], fill="#E0F2FE", bg="#091827")


def _draw_north_arrow(draw: ImageDraw.ImageDraw, fonts: dict, map_box: tuple[int, int, int, int]) -> None:
    x = map_box[2] - 70
    y = map_box[1] + 62
    draw.polygon([(x, y - 38), (x - 14, y + 8), (x, y), (x + 14, y + 8)], fill="#E8F4FF")
    draw.line((x, y, x, y + 42), fill="#E8F4FF", width=3)
    draw.text((x - 7, y - 60), "N", font=fonts["heading"], fill=TEXT)


def _draw_scale_bar(draw: ImageDraw.ImageDraw, fonts: dict, map_box: tuple[int, int, int, int], projection: _Projection) -> None:
    target_km = _nice_scale_km(projection.width_km / 5)
    width = projection.km_to_px(target_km)
    x0 = map_box[0] + 40
    y0 = map_box[3] - 48
    draw.rectangle((x0 - 10, y0 - 25, x0 + width + 55, y0 + 28), fill="#06101D", outline="#164E63")
    draw.line((x0, y0, x0 + width, y0), fill=TEXT, width=5)
    draw.line((x0, y0 - 9, x0, y0 + 9), fill=TEXT, width=3)
    draw.line((x0 + width, y0 - 9, x0 + width, y0 + 9), fill=TEXT, width=3)
    draw.text((x0, y0 + 11), "0", font=fonts["tiny"], fill=MUTED)
    draw.text((x0 + width - 12, y0 + 11), f"{target_km} km", font=fonts["tiny"], fill=MUTED)


def _draw_side_panel(draw: ImageDraw.ImageDraw, fonts: dict, box: tuple[int, int, int, int], report: dict) -> None:
    draw.rounded_rectangle(box, radius=10, fill="#06101D", outline="#164E63", width=2)
    x = box[0] + 24
    y = box[1] + 22
    draw.text((x, y), "LEGEND", font=fonts["heading"], fill=TEXT)
    y += 40
    for severity in ("critical", "high", "medium", "low"):
        style = SEVERITY_STYLES[severity]
        draw.rectangle((x, y, x + 32, y + 20), fill=style["fill"], outline=style["outline"], width=2)
        draw.text((x + 46, y), style["label"], font=fonts["body"], fill=TEXT)
        y += 29
    _legend_line(draw, fonts, x, y, CYAN, "Analysis boundary", dashed=False)
    y += 28
    _legend_marker(draw, fonts, x, y, "Facility marker")
    y += 28
    _legend_line(draw, fonts, x, y, BLUE, "Evacuation route", dashed=False, width=5)
    y += 42

    draw.line((x, y, box[2] - 24, y), fill="#164E63", width=1)
    y += 22
    draw.text((x, y), "IMPACT SNAPSHOT", font=fonts["heading"], fill=TEXT)
    y += 36
    stats = [
        ("Affected area", f"{report['analysis']['affected_area_km2']} km2"),
        ("Damage", f"{report['analysis']['damage_percent']}%"),
        ("Total zones", report["analysis"]["total_zones"]),
        ("Population", f"{report['impact']['population_affected']:,}"),
        ("Hospitals at risk", report["impact"]["hospitals_at_risk"]),
        ("Roads blocked", f"{report['impact']['roads_blocked_km']} km"),
        ("Schools affected", report["impact"]["schools_affected"]),
        ("Vulnerability", report["impact"]["vulnerability_score"]),
    ]
    for label, value in stats:
        draw.text((x, y), label.upper(), font=fonts["tiny"], fill=MUTED)
        draw.text((box[2] - 140, y - 2), str(value), font=fonts["body"], fill="#F8FAFC")
        y += 28

    y += 14
    draw.line((x, y, box[2] - 24, y), fill="#164E63", width=1)
    y += 22
    draw.text((x, y), "SYMBOL NOTES", font=fonts["heading"], fill=TEXT)
    y += 34
    notes = [
        "Risk polygons are mock demo outputs from the local Report Agent.",
        "Coordinates are WGS84; scale is approximate for situational reporting.",
    ]
    for line in notes:
        wrapped = _wrap_text(line, fonts["small"], 330)
        for wrapped_line in wrapped:
            draw.text((x, y), wrapped_line, font=fonts["small"], fill=MUTED)
            y += 19
        y += 6


def _draw_footer(draw: ImageDraw.ImageDraw, fonts: dict) -> None:
    draw.rounded_rectangle((35, 922, 1565, 970), radius=10, fill="#06101D", outline="#164E63", width=1)
    draw.text(
        (60, 938),
        "Generated by HazardMind Report Agent | Demo data | CRS: WGS84",
        font=fonts["body"],
        fill="#A5F3FC",
    )
    draw.text((1325, 938), "LOCAL DEMO ARTIFACT", font=fonts["body"], fill="#FDE68A")


def _draw_geojson_geometry(
    draw: ImageDraw.ImageDraw,
    geometry: dict | None,
    projection: _Projection,
    outline: str,
    width: int = 2,
    dash: bool = False,
) -> None:
    for polygon in _geometry_polygons(geometry):
        points = [projection.to_px(lon, lat) for lon, lat in polygon]
        if len(points) < 2:
            continue
        if dash:
            _draw_dashed_line(draw, points + [points[0]], outline, width)
        else:
            draw.line(points + [points[0]], fill=outline, width=width, joint="curve")


def _geometry_polygons(geometry: dict | None) -> list[list[list[float]]]:
    if not geometry:
        return []
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates", [])
    if geometry_type == "Polygon":
        return [coordinates[0]] if coordinates else []
    if geometry_type == "MultiPolygon":
        return [polygon[0] for polygon in coordinates if polygon]
    return []


def _legend_line(draw: ImageDraw.ImageDraw, fonts: dict, x: int, y: int, color: str, label: str, dashed: bool, width: int = 3) -> None:
    if dashed:
        _draw_dashed_line(draw, [(x, y + 10), (x + 42, y + 10)], color, width)
    else:
        draw.line((x, y + 10, x + 42, y + 10), fill=color, width=width)
    draw.text((x + 56, y), label, font=fonts["body"], fill=TEXT)


def _legend_marker(draw: ImageDraw.ImageDraw, fonts: dict, x: int, y: int, label: str) -> None:
    cx, cy = x + 18, y + 10
    draw.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill="#07111F", outline="#FF5B5B", width=3)
    draw.line((cx - 6, cy, cx + 6, cy), fill="#FF5B5B", width=3)
    draw.line((cx, cy - 6, cx, cy + 6), fill="#FF5B5B", width=3)
    draw.text((x + 56, y), label, font=fonts["body"], fill=TEXT)


def _draw_label(draw: ImageDraw.ImageDraw, anchor: tuple[int, int], text: str, font, fill: str = TEXT, bg: str = "#06101D") -> None:
    x, y = anchor
    bbox = draw.textbbox((x, y), text, font=font)
    pad = 5
    draw.rounded_rectangle(
        (bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad),
        radius=4,
        fill=bg,
        outline="#164E63",
    )
    draw.text((x, y), text, font=font, fill=fill)


def _draw_dashed_line(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], fill: str, width: int) -> None:
    for start, end in zip(points, points[1:]):
        x1, y1 = start
        x2, y2 = end
        length = math.hypot(x2 - x1, y2 - y1)
        if length == 0:
            continue
        dash = 12
        gap = 8
        step = dash + gap
        for distance in range(0, round(length), step):
            a = distance / length
            b = min(distance + dash, length) / length
            draw.line(
                (x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, x1 + (x2 - x1) * b, y1 + (y2 - y1) * b),
                fill=fill,
                width=width,
            )


def _draw_route_arrows(draw: ImageDraw.ImageDraw, points: list[tuple[int, int]], color: str) -> None:
    for start, end in zip(points, points[1:]):
        x1, y1 = start
        x2, y2 = end
        angle = math.atan2(y2 - y1, x2 - x1)
        mx = (x1 + x2) / 2
        my = (y1 + y2) / 2
        size = 12
        left = (mx - size * math.cos(angle - 0.55), my - size * math.sin(angle - 0.55))
        right = (mx - size * math.cos(angle + 0.55), my - size * math.sin(angle + 0.55))
        tip = (mx + size * math.cos(angle), my + size * math.sin(angle))
        draw.polygon([tip, left, right], fill=color)


def _wrap_text(text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    scratch = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    for word in words:
        candidate = f"{current} {word}".strip()
        if scratch.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _linspace(start: float, end: float, count: int) -> list[float]:
    if count <= 1:
        return [start]
    step = (end - start) / (count - 1)
    return [start + step * index for index in range(count)]


def _inset(box: tuple[int, int, int, int], amount: int) -> tuple[int, int, int, int]:
    return box[0] + amount, box[1] + amount, box[2] - amount, box[3] - amount


def _nice_scale_km(value: float) -> int:
    for candidate in (1, 2, 5, 10, 20, 50, 100):
        if candidate >= value:
            return candidate
    return 100
