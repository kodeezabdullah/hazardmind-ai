import os,sys
from pathlib import Path
_H=Path(r"d:\hazardmind-ai\tests\validation")
sys.path.insert(0,str(_H)); sys.path.insert(0,r"d:\hazardmind-ai\agents\satellite")
b=Path(r"d:\hazardmind-ai\agents\satellite\venv\Lib\site-packages\rasterio\proj_data")
os.environ["PROJ_LIB"]=str(b); os.environ["PROJ_DATA"]=str(b)
import numpy as np, yaml
from shapely.geometry import shape, mapping
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_geom
from rasterio.features import geometry_mask
import sar_change_detection as scd, aoi_pin, reference_loader
from processor import clip_to_polygon, stack_bands
CACHE=Path(r"C:\Users\Abdullaaa\AppData\Local\Temp\hazardmind-satellite\f521aed7-8712-49b6-97c6-4dc956adac61")
cfg=yaml.safe_load((_H/"reference_events"/"emsr692_kanalia.yaml").read_text(encoding="utf-8"))
loc=cfg["pipeline_location"]
with aoi_pin.pinned_aoi():
    from boundary import get_risk_city_boundaries, merge_risk_boundaries
    merged=merge_risk_boundaries(get_risk_city_boundaries(loc,[loc.split(",")[0].strip()]))
def clip(d): return clip_to_polygon(stack_bands({"VV":str(CACHE/d/"bands"/"VV.tiff")},"sentinel-1"),merged)
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
base=scd.build_baseline([scd.refined_lee(s) for s in pres])["baseline"]
ch=scd.log_ratio(scd.refined_lee(post["bands"]["VV"]),base)
val=np.isfinite(ch)
if post.get("mask") is not None: val&=post["mask"]
ref_geom,ref_crs=reference_loader.load_reference_geometry(
    cfg["reference_products"]["sentinel1"]["download_url"],
    cfg["reference_products"]["sentinel1"]["vector_layer_of_record"],"emsr692_kanalia")
ref_clip=ref_geom.intersection(shape(merged))
rr=shape(transform_geom("EPSG:4326",post["crs"],mapping(ref_clip)))
inref=geometry_mask([mapping(rr)],out_shape=shp,transform=post["transform"],invert=True)
fl=val&inref; dry=val&~inref
a=ch[fl]; d_=ch[dry]
print(f"flood px={a.size:,}  dry px={d_.size:,}")
print(f"flood: mean {a.mean():+.4f} median {np.median(a):+.4f} std {a.std():.4f}")
print(f"dry  : mean {d_.mean():+.4f} median {np.median(d_):+.4f} std {d_.std():.4f}")
sep=abs(a.mean()-d_.mean())/np.sqrt((a.std()**2+d_.std()**2)/2)
print(f"\nCohen's d (flood vs dry separation) = {sep:.4f}")
# ROC AUC via rank statistic  (probability a flood px is DARKER than a dry px)
from scipy.stats import rankdata
allv=np.concatenate([a,d_]); r=rankdata(-allv)  # more negative = more flood-like
auc=(r[:a.size].sum()-a.size*(a.size+1)/2)/(a.size*d_.size)
print(f"ROC AUC (single-threshold ceiling) = {auc:.4f}   (0.5 = no signal)")
# best achievable F1 by ANY global threshold on this change image
best=(0,0,0,0)
for t in np.arange(-4,0.5,0.05):
    m=val&(ch<=t)
    tp=(m&inref).sum(); fp=(m&~inref).sum(); fn=(inref&val&~m).sum()
    if tp==0: continue
    p=tp/(tp+fp); rc=tp/(tp+fn); f1=2*p*rc/(p+rc)
    if f1>best[0]: best=(f1,t,p,rc)
print(f"\nBEST POSSIBLE pixel F1 by ANY global cut: F1={best[0]:.4f} at {best[1]:+.2f} dB (P={best[2]:.4f} R={best[3]:.4f})")
print(f"reference covers {100*inref[val].mean():.1f}% of the valid AOI")

print("\n=== IS -1.0 dB A REAL IMPROVEMENT, OR NOISE-FITTING? ===")
# Baseline to beat: label the ENTIRE valid AOI as flood (zero skill).
tp=inref[val].sum(); fp=(~inref[val]).sum()
p0=tp/(tp+fp); r0=1.0; f0=2*p0/(p0+1)
print(f"  ZERO-SKILL (label whole AOI flood): P={p0:.4f} R={r0:.4f} F1={f0:.4f}")
for t in (-1.0,-1.782,-3.0):
    m=val&(ch<=t)
    tp=(m&inref).sum(); fp=(m&~inref).sum(); fn=(inref&val&~m).sum()
    p=tp/(tp+fp) if tp+fp else 0; rc=tp/(tp+fn) if tp+fn else 0
    f1=2*p*rc/(p+rc) if p+rc else 0
    lift=p/p0
    print(f"  cut {t:+.3f} dB: P={p:.4f} (lift over chance {lift:.2f}x) R={rc:.4f} F1={f1:.4f}")
print("\n  A precision LIFT near 1.0x means the detector is no better than")
print("  guessing. Only a lift comfortably >1 indicates real skill.")
