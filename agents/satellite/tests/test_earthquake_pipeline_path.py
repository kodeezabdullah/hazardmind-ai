"""Prove SAR damage detection runs through the REAL calculate_indices path."""
import os, sys
from pathlib import Path
_H=Path(r"d:\hazardmind-ai")
sys.path.insert(0,str(_H/"agents"/"satellite"))
b=_H/"agents"/"satellite"/"venv"/"Lib"/"site-packages"/"rasterio"/"proj_data"
os.environ["PROJ_LIB"]=str(b); os.environ["PROJ_DATA"]=str(b)
import numpy as np
from affine import Affine
import processor as p

rng=np.random.default_rng(3); N=240
built=np.zeros((N,N),bool); built[30:210,30:210]=True
dmg=np.zeros((N,N),bool);   dmg[70:150,70:150]=True
# Built-up spectra so IBI marks the district (B11>B08, red present)
b03=np.full((N,N),0.06,dtype="float32"); b04=np.full((N,N),0.04,dtype="float32")
b08=np.full((N,N),0.45,dtype="float32"); b11=np.full((N,N),0.22,dtype="float32")
b03[built],b04[built],b08[built],b11[built]=0.12,0.15,0.20,0.30
pre_vv=(300*rng.gamma(4.4,1/4.4,(N,N))).astype("float32")
pre_vh=(60*rng.gamma(4.4,1/4.4,(N,N))).astype("float32")
post_vv=pre_vv.copy(); post_vh=pre_vh.copy()
post_vv[dmg]*=0.45   # double-bounce destroyed
post_vh[dmg]*=2.6    # volume scattering -> depolarisation
clipped={"bands":{"VV":post_vv,"VH":post_vh,"B03":b03,"B04":b04,"B08":b08,"B11":b11},
         "mask":np.ones((N,N),bool),
         "transform":Affine(10.0,0,500000.0,0,-10.0,4000000.0),"crs":None}
stack=p._PreEventStack([pre_vv]); stack.vh=[pre_vh]
res=p.calculate_indices(clipped,"sentinel-1","earthquake",pre_event_vv=stack)
print("=== THROUGH THE REAL calculate_indices ENTRY POINT ===")
for k in ("index_type","index_units","index_calibrated","water_percent",
          "threshold_method","built_up_available"):
    print(f"  {k:22} {res.get(k)}")
eq=res.get("earthquake_damage") or {}
print("  --- earthquake_damage block ---")
for k,v in eq.items(): print(f"    {k:34} {str(v)[:105]}")
cls=res["classification_array"]
print(f"  classification: damage px={int((cls==3).sum())}, intact={int((cls==0).sum())}")
ok = res.get("index_type")=="SAR_DAMAGE" and eq.get("polarimetric_evidence_available")
print("\nRESULT:", "PASS — SAR damage detection ran through the pipeline entry point"
      if ok else "FAIL — did not reach the damage path")
