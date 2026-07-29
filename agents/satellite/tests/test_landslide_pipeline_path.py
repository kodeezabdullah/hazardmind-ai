"""Prove the landslide detector runs through calculate_indices on REAL pixels.

Uses the cached Keramidi S1... no — landslide needs OPTICAL. Build a real-shaped
S2 cube and drive the REAL calculate_indices entry point (not the detector
directly), so this proves the WIRING carries a pre-event NDVI into a scar map.
"""
import os, sys
from pathlib import Path
_H=Path(r"d:\hazardmind-ai")
sys.path.insert(0,str(_H/"agents"/"satellite"))
b=_H/"agents"/"satellite"/"venv"/"Lib"/"site-packages"/"rasterio"/"proj_data"
os.environ["PROJ_LIB"]=str(b); os.environ["PROJ_DATA"]=str(b)
import numpy as np
from affine import Affine
import processor as p

N=300
yy,xx=np.mgrid[0:N,0:N].astype("float32")
# Real hillslope: falls east -> aspect 90, ~35 deg
dem=(N-xx)*7.0
# Pre-event: healthy vegetation. B08 high, B04 low.
b04=np.full((N,N),0.04,dtype="float32")
b08=np.full((N,N),0.45,dtype="float32")
b03=np.full((N,N),0.06,dtype="float32")
b11=np.full((N,N),0.22,dtype="float32")
pre_ndvi=(b08-b04)/(b08+b04)
# Post-event: a tapering downslope scar (bare soil spectra)
post04=b04.copy(); post08=b08.copy()
for i,x in enumerate(range(80,220)):
    half=max(1,int(16*(1-i/140.0)))
    post04[150-half:150+half+1,x]=0.26
    post08[150-half:150+half+1,x]=0.30
clipped={
 "bands":{"B03":b03,"B04":post04,"B08":post08,"B11":b11},
 "mask":np.ones((N,N),bool),
 "transform":Affine(10.0,0,500000.0,0,-10.0,4000000.0),
 "crs":None,
}
res=p.calculate_indices(clipped,"sentinel-2","landslide",
                        pre_event_ndvi=pre_ndvi,dem=dem)
print("=== THROUGH THE REAL calculate_indices ENTRY POINT ===")
for k in ("index_type","index_units","index_calibrated","water_percent",
          "threshold_method","affected_mean_index"):
    print(f"  {k:22} {res.get(k)}")
ld=res.get("landslide_detection") or {}
print("  --- landslide_detection block ---")
for k,v in ld.items(): print(f"    {k:30} {str(v)[:110]}")
print(f"  scars returned: {len(res.get('scars') or [])}")
cls=res["classification_array"]
print(f"  classification: scar px={int((cls==3).sum())}, safe={int((cls==0).sum())}")
ok = res.get("index_type")=="NDVI_CHANGE" and (res.get("scars") or [])
print("\nRESULT:", "PASS — bi-temporal scar detection ran through the pipeline entry point"
      if ok else "FAIL — did not reach the scar path")
