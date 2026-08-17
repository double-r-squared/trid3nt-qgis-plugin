#!/usr/bin/env python3
# culvert-through-embankment A/B/C seam proof figure.
import h5py, numpy as np, json
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.gridspec import GridSpec

P="/home/nate/hecras_probe2025"
DEP="/Results/Output Blocks/Base Output/2D Flow Areas/Base Mesh/Cell Depth"
probe=json.load(open(f"{P}/cv_pass/culvert_probe.json"))
y0,y1,relev=probe["ridge_y0"],probe["ridge_y1"],probe["ridge_elev"]
bx,uy,dy,diam=probe["barrel_x"],probe["us_y"],probe["ds_y"],probe["diameter"]

with h5py.File(f"{P}/cv_pass_result.h5","r") as f:
    xy=f["/Geometry/2D Flow Areas/Base Mesh/Cell Coordinates"][:]
X,Y=xy[:,0],xy[:,1]
us=Y>y1+1e-6; ds=Y<y0-1e-6
A_cell=100.0; dt_rep=40.0

# Regular structured mesh (StructChannel: 60x300 m, 10 m cells -> 6x30). Render each
# wet cell as its FILLED footprint (pcolormesh on the cell grid), never cell-center
# scatter dots -- honest coarseness, no interpolation across the 10 m cells.
CELL=10.0
NX,NY=6,30
x_edges=np.arange(NX+1)*CELL
y_edges=np.arange(NY+1)*CELL
col=np.clip((X/CELL).astype(int),0,NX-1)
row=np.clip((Y/CELL).astype(int),0,NY-1)

def to_grid(d):
    g=np.full((NY,NX),np.nan)
    g[row,col]=d
    return g

cases=[("C  no embankment\n(no ridge, no culvert)","cv_free"),
       ("B  embankment only\n(ridge, no culvert)","cv_block"),
       ("A  embankment + culvert\n(ridge + barrel)","cv_pass")]
depth={}; series={}
for _,c in cases:
    with h5py.File(f"{P}/{c}_result.h5","r") as f:
        d=f[DEP][:]
    depth[c]=d[-1,:]; series[c]=d[:,us].mean(axis=1)
vmax=max(depth[c].max() for _,c in cases)

fig=plt.figure(figsize=(15,8.5))
gs=GridSpec(2,3,height_ratios=[2.3,1.0],hspace=0.34,wspace=0.22,
            left=0.055,right=0.965,top=0.9,bottom=0.09)
fig.suptitle("HEC-RAS 2025 managed engine -- 2D culvert-through-embankment seam proof (ADR 0251)\n"
             "synthetic seam-probe: 60x300 m channel, inflow 4 m3/s (top) -> tailwater 1.0 m (bottom); "
             "ridge 6 m across y=[140,160]; circular barrel diam 2 m under the ridge",
             fontsize=11.5,y=0.985)

# top row: plan-view depth maps
for i,(label,c) in enumerate(cases):
    ax=fig.add_subplot(gs[0,i])
    grid=to_grid(depth[c])
    sc=ax.pcolormesh(x_edges,y_edges,np.ma.masked_invalid(grid),cmap="viridis",
                      vmin=0,vmax=vmax,shading="flat",edgecolors="white",linewidth=0.3)
    # ridge band (only B,C... draw on ridge cases)
    if c!="cv_free":
        ax.add_patch(Rectangle((-3,y0),66,y1-y0,facecolor="saddlebrown",alpha=0.35,zorder=1))
        tx=16 if c=="cv_pass" else 30
        ax.text(tx,(y0+y1)/2,f"embankment ridge {relev:.0f} m",ha="center",va="center",
                fontsize=8,color="#3a1d00",fontweight="bold",zorder=5)
    if c=="cv_pass":
        ax.plot([bx,bx],[dy,uy],color="red",lw=2.4,zorder=6)
        ax.plot([bx,bx],[dy,uy],color="red",marker="o",ms=5,zorder=6)
        ax.text(bx+3,uy+14,"culvert barrel",color="red",fontsize=8,fontweight="bold",
                ha="center",zorder=7)
    ax.set_title(label,fontsize=10.5)
    ax.set_xlim(-4,64); ax.set_ylim(-4,304)
    ax.set_xlabel("x (m)");
    if i==0: ax.set_ylabel("y (m)   inflow top -> tailwater bottom")
    ax.text(0.5,-0.14,f"upstream mean depth = {depth[c][us].mean():.2f} m",transform=ax.transAxes,
            ha="center",fontsize=9.5,fontweight="bold",
            color=("green" if c=="cv_free" else "firebrick" if c=="cv_block" else "darkorange"))
cb=fig.colorbar(sc,ax=fig.axes[:3],fraction=0.03,pad=0.01)
cb.set_label("water depth (m)")

# bottom-left: upstream depth vs time
axt=fig.add_subplot(gs[1,0:2])
t=np.arange(len(series["cv_free"]))*dt_rep/60.0
col={"cv_free":"green","cv_block":"firebrick","cv_pass":"darkorange"}
lab={"cv_free":"C free (steady, flows away)","cv_block":"B embankment only (PONDS, unbounded)",
     "cv_pass":"A embankment+culvert (STEADY -- barrel conveys inflow)"}
for c in ["cv_free","cv_block","cv_pass"]:
    axt.plot(t,series[c],color=col[c],lw=2.3,label=lab[c])
axt.axhline(relev,color="saddlebrown",ls="--",lw=1,alpha=0.6)
axt.text(t[-1],relev+0.05,"ridge crest 6 m",ha="right",fontsize=8,color="saddlebrown")
axt.set_xlabel("time (min)"); axt.set_ylabel("upstream mean depth (m)")
axt.set_title("Upstream ponding vs steady state",fontsize=10)
axt.legend(fontsize=8.5,loc="upper left"); axt.grid(alpha=0.3)

# bottom-right: mass-balance bars
axb=fig.add_subplot(gs[1,2])
def rate(c):
    with h5py.File(f"{P}/{c}_result.h5","r") as f: d=f[DEP][:]
    v=d[:,us].sum(axis=1)*A_cell; n=len(v); w=slice(3*n//4,n)
    tt=np.arange(n)*dt_rep
    return np.polyfit(tt[w],v[w],1)[0]
rates=[rate("cv_free"),rate("cv_block"),rate("cv_pass")]
bars=axb.bar(["C","B","A"],rates,color=["green","firebrick","darkorange"])
axb.axhline(4.0,color="k",ls=":",lw=1); axb.text(2.4,4.05,"inflow 4",fontsize=8)
axb.set_ylabel("upstream storage rate dV/dt (m3/s)")
axb.set_title("Mass balance: where the inflow goes",fontsize=10)
for b,r in zip(bars,rates):
    axb.text(b.get_x()+b.get_width()/2,r+0.08,f"{r:+.2f}",ha="center",fontsize=9,fontweight="bold")
axb.text(0.5,-0.32,"B traps ~inflow (ponding).  A ~0 -> barrel conveys it downstream (arrival by conservation).",
         transform=axb.transAxes,ha="center",fontsize=7.6,style="italic")

out="/home/nate/Documents/trid3nt-local/docs/proof/templates/hecras_culvert_embankment_flow_seam_probe_abc.png"
fig.savefig(out,dpi=120,bbox_inches="tight")
print("wrote",out)
