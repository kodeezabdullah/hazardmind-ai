import os,sys
from pathlib import Path
_H=Path(r"d:\hazardmind-ai\tests\validation")
sys.path.insert(0,str(_H)); sys.path.insert(0,r"d:\hazardmind-ai\agents\satellite")
b=Path(r"d:\hazardmind-ai\agents\satellite\venv\Lib\site-packages\rasterio\proj_data")
os.environ["PROJ_LIB"]=str(b); os.environ["PROJ_DATA"]=str(b)
import numpy as np, yaml
from shapely.geometry import shape, mapping
from shapely.ops import unary_union
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_geom
from rasterio.features import shapes as rio_shapes
import sar_change_detection as scd, aoi_pin, reference_loader, metrics
from processor import clip_to_polygon, stack_bands
CACHE=Path(r"C:\Users\Abdullaaa\AppData\Local\Temp\hazardmind-satellite\f521aed7-8712-49b6-97c6-4dc956adac61")
cfg=yaml.safe_load((_H/"reference_events"/"emsr692_kanalia.yaml").read_text(encoding="utf-8"))
loc=cfg["pipeline_location"]
with aoi_pin.pinned_aoi():
    from boundary import get_risk_city_boundaries, merge_risk_boundaries
    merged=merge_risk_boundaries(get_risk_city_boundaries(loc,[loc.split(",")[0].strip()]))
def clip(d):
    return clip_to_polygon(stack_bands({"VV":str(CACHE/d/"bands"/"VV.tiff")},"sentinel-1"),merged)
post=clip("t1"); shp=post["bands"]["VV"].shape
pres=[]
for d in ("pre_0","pre_1","pre_2"):
    c=clip(d); vv=c["bands"]["VV"]
    if vv.shape!=shp:
        dest=np.full(shp,np.nan,dtype="float32")
        reproject(source=vv.astype("float32"),destination=dest,src_transform=c["transform"],
            src_crs=c["crs"],dst_transform=post["transform"],dst_crs=post["crs"],
            resampling=Resampling.bilinear,src_nodata=np.nan,dst_nodata=np.nan)
        vv=dest
    pres.append(vv)
ref_geom,ref_crs=reference_loader.load_reference_geometry(
    cfg["reference_products"]["sentinel1"]["download_url"],
    cfg["reference_products"]["sentinel1"]["vector_layer_of_record"],"emsr692_kanalia")
ref_clip=ref_geom.intersection(shape(merged))
def score(mask):
    if mask is None or not mask.any(): return None
    polys=[shape(g) for g,v in rio_shapes(mask.astype("uint8"),mask=mask,transform=post["transform"]) if v==1]
    pred=unary_union(polys)
    if post.get("crs") is not None:
        pred=shape(transform_geom(post["crs"],"EPSG:4326",mapping(pred)))
    return metrics.compute_extent_metrics(pred,"EPSG:4326",ref_clip,ref_crs)
print(f"{'variant':38} {'IoU':>8} {'P':>8} {'R':>8} {'F1':>8} {'pred km2':>9}")
print("-"*82)
for label,thr in [("KI-tiled (production)",None),("fixed -1.0 dB",-1.0),
                  ("fixed -1.5 dB",-1.5),("fixed -2.0 dB",-2.0),("fixed -3.0 dB",-3.0)]:
    cd=scd.detect_flood_change(post["bands"]["VV"],pres,valid_mask=post.get("mask"),
                               orbit_direction="DESCENDING",direction="drop")
    if thr is not None:
        base=scd.build_baseline([scd.refined_lee(s) for s in pres])["baseline"]
        ch=scd.log_ratio(scd.refined_lee(post["bands"]["VV"]),base)
        val=np.isfinite(ch)
        if post.get("mask") is not None: val&=post["mask"]
        m=scd.morphological_cleanup(val&(ch<=thr))
    else:
        m=cd["flood_mask"]
    s=score(m)
    if s: print(f"{label:38} {s.iou:8.4f} {s.precision:8.4f} {s.recall:8.4f} {s.f1:8.4f} {s.predicted_area_km2:9.2f}")
    else: print(f"{label:38} {'0 zones':>8}")
# no-morphology variant
base=scd.build_baseline([scd.refined_lee(s) for s in pres])["baseline"]
ch=scd.log_ratio(scd.refined_lee(post["bands"]["VV"]),base)
val=np.isfinite(ch)
if post.get("mask") is not None: val&=post["mask"]
for label,m in [("KI cut, NO morphology",val&(ch<=-1.782)),
                ("no speckle filter, -1.782",None)]:
    if m is None:
        b2=scd.build_baseline(pres)["baseline"]
        c2=scd.log_ratio(post["bands"]["VV"],b2)
        v2=np.isfinite(c2)
        if post.get("mask") is not None: v2&=post["mask"]
        m=scd.morphological_cleanup(v2&(c2<=-1.782))
    s=score(m)
    if s: print(f"{label:38} {s.iou:8.4f} {s.precision:8.4f} {s.recall:8.4f} {s.f1:8.4f} {s.predicted_area_km2:9.2f}")
