"""IoU / precision / recall / F1 of a predicted flood extent against an EMS
reference delineation polygon.

Both geometries are reprojected to a common **equal-area** CRS before any
area/overlap computation. Two rasters/vectors at different native
resolutions (the reference delineation is a hand/semi-automatically
digitized vector at whatever the source imagery's resolution was — VHR
optical, Landsat, or Sentinel; the prediction is a rasterize-then-vectorize
product off a 10 m/20 m Sentinel grid) are NOT resampled onto a shared grid
before comparison. Instead we compare the geometries directly in an
equal-area CRS: intersection/union area is exact for the vector geometries
themselves, so this avoids injecting a fake extra quantization step neither
source actually has. The tradeoff: a coarser-resolution reference's polygon
boundary is a smoother/blockier line than the prediction's, so boundary-length
disagreement (not area disagreement) is inflated a bit by resolution mismatch
alone — noted in every report so it isn't mistaken for a real model error.

Equal-area CRS choice: EPSG:6933 (world Cylindrical Equal-Area), the same
projection `agents/satellite/processor.py::_polygon_area_km2` already uses
for its own area math — keeping the same projection here means these metrics
are directly comparable to the pipeline's own self-reported `affected_area_km2`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import geopandas as gpd
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

EQUAL_AREA_CRS = "EPSG:6933"


@dataclass
class ExtentMetrics:
    iou: float
    precision: float
    recall: float
    f1: float
    predicted_area_km2: float
    reference_area_km2: float
    intersection_area_km2: float
    union_area_km2: float

    def as_dict(self) -> dict:
        return {
            "iou": round(self.iou, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "predicted_area_km2": round(self.predicted_area_km2, 3),
            "reference_area_km2": round(self.reference_area_km2, 3),
            "intersection_area_km2": round(self.intersection_area_km2, 3),
            "union_area_km2": round(self.union_area_km2, 3),
        }


def _to_equal_area(geom: BaseGeometry, src_crs: str) -> BaseGeometry:
    gs = gpd.GeoSeries([geom], crs=src_crs)
    return gs.to_crs(EQUAL_AREA_CRS).iloc[0]


def compute_extent_metrics(
    predicted_geom: BaseGeometry,
    predicted_crs: str,
    reference_geom: BaseGeometry,
    reference_crs: str,
) -> ExtentMetrics:
    """Compare a predicted flood-extent geometry against a reference
    delineation geometry. Both are reprojected to EPSG:6933 first; all area
    figures in the result are km^2 in that equal-area CRS, not native-CRS
    degrees^2 or a resolution-dependent pixel count.
    """
    pred_ea = _to_equal_area(predicted_geom, predicted_crs)
    ref_ea = _to_equal_area(reference_geom, reference_crs)

    if not pred_ea.is_valid:
        pred_ea = pred_ea.buffer(0)
    if not ref_ea.is_valid:
        ref_ea = ref_ea.buffer(0)

    intersection = pred_ea.intersection(ref_ea)
    union = pred_ea.union(ref_ea)

    pred_area = pred_ea.area / 1e6
    ref_area = ref_ea.area / 1e6
    inter_area = intersection.area / 1e6
    union_area = union.area / 1e6

    iou = inter_area / union_area if union_area > 0 else 0.0
    precision = inter_area / pred_area if pred_area > 0 else 0.0
    recall = inter_area / ref_area if ref_area > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )

    return ExtentMetrics(
        iou=iou,
        precision=precision,
        recall=recall,
        f1=f1,
        predicted_area_km2=pred_area,
        reference_area_km2=ref_area,
        intersection_area_km2=inter_area,
        union_area_km2=union_area,
    )


@dataclass
class PermanentWaterSplit:
    """Metrics computed both including and excluding permanent-water pixels.

    The pipeline has no permanent-water mask (see agents/satellite/CLAUDE.md
    and ANALYSIS.md — NDWI/SAR flood detection classifies open water whether
    it was already a river/lake or newly flooded). Any reference delineation
    that includes permanently-wet channels will therefore over-credit the
    pipeline for water it did not "detect as flood" so much as always render
    as water. `excluding_permanent_water` subtracts a supplied permanent-water
    polygon from BOTH predicted and reference geometries before scoring, so
    the two metric sets bound the real answer: `including` is optimistic
    (rivers count as correctly-detected flood), `excluding` is pessimistic
    (a genuinely flooded river channel is invisible to both sides equally).
    """

    including_permanent_water: ExtentMetrics
    excluding_permanent_water: Optional[ExtentMetrics]
    permanent_water_source: Optional[str] = None
    note: str = field(default="")


def compute_with_permanent_water_split(
    predicted_geom: BaseGeometry,
    predicted_crs: str,
    reference_geom: BaseGeometry,
    reference_crs: str,
    permanent_water_geom: Optional[BaseGeometry] = None,
    permanent_water_crs: Optional[str] = None,
    permanent_water_source: Optional[str] = None,
) -> PermanentWaterSplit:
    including = compute_extent_metrics(
        predicted_geom, predicted_crs, reference_geom, reference_crs
    )

    if permanent_water_geom is None:
        return PermanentWaterSplit(
            including_permanent_water=including,
            excluding_permanent_water=None,
            permanent_water_source=None,
            note=(
                "No permanent-water mask supplied for this event — "
                "excluding-permanent-water metrics could not be computed. "
                "Treat the including-permanent-water numbers as an upper "
                "bound, not the real flood-detection accuracy."
            ),
        )

    pw_ea = _to_equal_area(permanent_water_geom, permanent_water_crs or reference_crs)
    pred_ea = _to_equal_area(predicted_geom, predicted_crs)
    ref_ea = _to_equal_area(reference_geom, reference_crs)

    pred_excl = pred_ea.difference(pw_ea)
    ref_excl = ref_ea.difference(pw_ea)

    excluding = compute_extent_metrics(
        pred_excl, EQUAL_AREA_CRS, ref_excl, EQUAL_AREA_CRS
    )

    return PermanentWaterSplit(
        including_permanent_water=including,
        excluding_permanent_water=excluding,
        permanent_water_source=permanent_water_source,
        note=(
            "excluding_permanent_water subtracts the supplied permanent-water "
            f"polygon ({permanent_water_source}) from both predicted and "
            "reference geometries before scoring."
        ),
    )


def union_of_class_polygons(geoms: list[BaseGeometry]) -> BaseGeometry:
    """Merge a list of per-class hazard-zone polygons (e.g. the pipeline's
    vectorize_classification output, one polygon per severity class) into a
    single flood-extent geometry for IoU comparison against a reference that
    doesn't distinguish severity classes."""
    valid = [g for g in geoms if g is not None and not g.is_empty]
    if not valid:
        from shapely.geometry import Polygon

        return Polygon()
    return unary_union(valid)
