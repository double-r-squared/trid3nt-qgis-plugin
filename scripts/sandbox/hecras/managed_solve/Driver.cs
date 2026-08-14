using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Ras.Synthetics;
using Ras.Layers;
using Ras.Layers.BoundaryConditions;
using Ras.Engine;
using Ras.Hydraulics;
using Ras.Hydraulics.Structures;
using Geospatial.Vectors;
using Geospatial.PairedData;
using Geospatial.GDALAssist;

// ADR 0209 -- 2025 managed-engine rain-on-grid authoring.
//
// Two authoring modes over the SAME proven Save()+precip+NValue sequence that
// produced the rain3 solvable project (ADR 0207):
//   rain    <dir> <rate>       -- closed flat basin, uniform constant precip.
//                                 Units calibration + mass check (no outflow BC:
//                                 depth-rise rate == precip rate, so the recorded
//                                 rise directly calibrates ConstantValue -> mm/hr).
//   realrog <spec.json>        -- REAL catchment: a structured 2D area over the
//                                 fetched-AOI terrain (the exported synthetic
//                                 Terrain.tif is OVERWRITTEN host-side by the
//                                 reprojected real DEM, aligned in local SI m),
//                                 constant design-storm precip, a NormalDepth
//                                 outlet BC on the pour-point wall.
//
// Rain is applied in-memory by PrecipitationLayer.InitializeComputeDriver; in an
// SI project ConstantValue IS the rate in mm/hr (scale = 1/3600 * 0.001 -> m/s,
// decoded from PrecipitationLayer.cs). No 6.6 hydrology scaffold, no Windows.

// Rain-only closed basin: no BC lines -> depth rises uniformly with the rain.
class RainBox : InOutPlanarParams
{
    public override BoundaryConditionLine GetUpstreamBC(Ras.Layers.BoundaryCondition bc, Polyline pl) => null;
    public override BoundaryConditionLine GetDownstreamBC(Ras.Layers.BoundaryCondition bc, Polyline pl) => null;
}

// Real-catchment rain-on-grid params. A structured grid over the AOI extent
// (local SI metres, origin 0,0); the associated terrain is the real DEM tif the
// host overwrites into Terrains/. One NormalDepth outlet BC on the named wall.
class RealTerrainRoG : Ras.Synthetics.BasicRectangleParams
{
    public string OutletEdge = "s";     // n|s|e|w -- the wall the catchment drains to
    public double OutletSlope = 0.05;   // NormalDepth energy/friction slope
    public double OutletStage = 0.0;    // ConstantStage outlet tailwater (bed elev, m)
    public string OutletBc = "stage";   // "stage" (proven external) | "normal_depth"
    public bool Diffusion = true;       // Diffusion Wave (default) vs full SWE

    // ADR 0210 -- paper-style dynamic resolution. When RefineDir is set the mesh is
    // built by MeshFactory.TryCreateMesh from a graded cell-center seed cloud +
    // channel breaklines (host-authored by rog_refine.py in this LOCAL SI frame),
    // NOT the uniform structured MeshFactory.FromExtent. The seeds ARE the cell
    // sizing (coarse background grading to fine channel); breaklines magnetize
    // facepoints onto the channel. Everything else (terrain overwrite, precip,
    // outlet BC, prepare, solve) is unchanged -- the extent still equals SystemExtent.
    public string RefineDir = null;
    public bool SplitExternalFaces = false;   // blue-noise interior needs no external
                                              // facepoint splitting; ON over-splits the
                                              // irregular perimeter cells past 8 faces
    private Mesh _refinedMesh;

    public override Mesh CreateMesh()
    {
        if (RefineDir == null)
            return base.CreateMesh();
        if (_refinedMesh != null)
            return _refinedMesh;
        Polygon perim = Polygon.FromExtent(SystemExtent);
        List<Point> seed0 = ReadPts(Path.Combine(RefineDir, "seeds.f64"));
        IList<Polyline> breaks = ReadBreaklines(Path.Combine(RefineDir, "breaklines.json"));
        var mgp = MeshGenerationParams.Default();
        mgp.CreateVirtualCells = false;
        mgp.SplitExternalFaces = SplitExternalFaces;
        // The graded seeds leave a few >8-sided cells at the size transition that HEC
        // hard-rejects (host-side degree repair relieves most, but HEC's face-collapse --
        // not raw Delaunay degree -- sets the final count, so a handful survive and vary
        // with the seed set). A TINY seed perturbation yields a different Delaunay/collapse
        // that clears them; retry with a growing deterministic jitter until it meshes.
        Mesh mesh = null; MeshError err = null;
        int attempts = 12;
        for (int a = 0; a < attempts; a++)
        {
            List<Point> seeds = a == 0 ? seed0 : DropSome(seed0, a);
            bool ok = MeshFactory.TryCreateMesh(perim, seeds, breaks, out mesh, out err, mgp, null);
            int nbad = err?.BadCells?.Count ?? -1;
            Console.WriteLine($"[mesh] refined TryCreateMesh attempt={a} ok={ok} status={err?.Status} " +
                              $"badcells={nbad} cells={mesh?.CellCount} faces={mesh?.FaceCount} " +
                              $"seeds={seeds.Count} breaklines={breaks.Count}");
            if (mesh != null) { _refinedMesh = mesh; return mesh; }
        }
        throw new Exception("refined TryCreateMesh failed after " + attempts +
                            " decimation attempts: " + err?.StatusMessage);
    }

    // Deterministic random seed DROP (a growing fraction with the attempt) -- the residual
    // >8-sided cell after the host-side crowding relief is a specific seed configuration;
    // dropping a few seeds re-triangulates it to <= 8 without touching the resolution field
    // (a fraction of a percent of >=22 m cells). Jitter/relaxation left it unchanged; a
    // topology change clears it.
    static List<Point> DropSome(List<Point> pts, int attempt)
    {
        var rng = new Random(9200 + attempt);
        double frac = 0.004 * attempt;               // 0.4%, 0.8%, ... dropped
        var outp = new List<Point>(pts.Count);
        foreach (var p in pts)
            if (rng.NextDouble() >= frac)
                outp.Add(p);
        return outp;
    }

    static List<Point> ReadPts(string path)
    {
        var bytes = File.ReadAllBytes(path);
        int n = bytes.Length / 16;
        var pts = new List<Point>(n);
        for (int i = 0; i < n; i++)
            pts.Add(new Point(BitConverter.ToDouble(bytes, i * 16),
                              BitConverter.ToDouble(bytes, i * 16 + 8)));
        return pts;
    }

    static IList<Polyline> ReadBreaklines(string path)
    {
        var list = new List<Polyline>();
        if (!File.Exists(path))
            return list;
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        foreach (var pl in doc.RootElement.EnumerateArray())
        {
            var pts = new List<Point>();
            foreach (var pt in pl.EnumerateArray())
                pts.Add(new Point(pt[0].GetDouble(), pt[1].GetDouble()));
            if (pts.Count >= 2)
                list.Add(new Polyline(pts));
        }
        return list;
    }

    public override void InitPlanOptions(Plan pl)
    {
        pl.SolverType = SolverType.CPU;
        pl.EquationSet = Diffusion ? SolverControl.EquationSet.DWE : SolverControl.EquationSet.SWE;
    }

    public override List<BoundaryConditionLine> GetBCLines(Ras.Layers.BoundaryCondition bc)
    {
        Extent ext = CreateMesh().Extent;
        Segment wall = OutletEdge switch
        {
            "n" => ext.TopWall,
            "e" => ext.RightWall,
            "w" => ext.LeftWall,
            _   => ext.BottomWall,
        };
        // The BC line must PROTRUDE past the wall corners so its endpoints fall
        // OUTSIDE the mesh perimeter -- otherwise TryIdentifyInternalExternal sees
        // the perimeter polygon fuzzily CONTAIN the line and classifies it INTERNAL
        // ("only Flow is supported for internal boundary conditions"). Extending it
        // beyond the corners makes ContainsFuzzy fail -> external (snapped to the
        // wall's perimeter faces). Mirrors the HEC GUI (draw the line past the edge).
        Polyline pl = Polyline.FromSegment(wall.Scale(-0.05, 1.05));
        BoundaryConditionLine outlet = OutletBc == "normal_depth"
            ? BoundaryConditionLine.NormalDepth(bc, pl, OutletSlope)
            : BoundaryConditionLine.ConstantStage(bc, pl, OutletStage);
        outlet.Name = "Outlet";
        return new List<BoundaryConditionLine> { outlet };
    }
}

// Inflow channel for the ADR 0249 structure A/B. Same physics as InOutPlanarParams
// (ramped inflow at the top wall, constant tailwater stage at the bottom) but the BC
// lines PROTRUDE past the mesh corners so TryIdentifyInternalExternal classes them
// EXTERNAL -- the base 0.01/0.99 inset lines are seen as INTERNAL, and a Stage BC on an
// internal line is rejected ("only Flow is supported for internal boundary conditions").
class StructChannel : InOutPlanarParams
{
    public override List<BoundaryConditionLine> GetBCLines(Ras.Layers.BoundaryCondition bc)
    {
        Extent ext = CreateMesh().Extent;
        Polyline up = Polyline.FromSegment(ext.TopWall.Scale(-0.05, 1.05));
        Polyline dn = Polyline.FromSegment(ext.BottomWall.Scale(-0.05, 1.05));
        BoundaryConditionLine upBC = GetUpstreamBC(bc, up); upBC.Name = "Upstream";
        BoundaryConditionLine dnBC = GetDownstreamBC(bc, dn); dnBC.Name = "Downstream";
        return new List<BoundaryConditionLine> { upBC, dnBC };
    }
}

class Driver
{
    static int Main(string[] args)
    {
        GDALSetup.InitializeMultiplatform();
        string mode = args.Length > 0 ? args[0] : "rain";
        if (mode == "realrog")
            return RealRog(args[1]);
        if (mode == "meshprobe")
            return MeshProbe(args[1]);
        if (mode == "structdemo")
            return StructDemo(args[1],
                              args.Length > 2 && (args[2] == "1" || args[2] == "weir"),
                              args.Length > 3 ? double.Parse(args[3]) : 2.0);
        return Rain(args.Length > 1 ? args[1] : "/probe/rain",
                    args.Length > 2 ? float.Parse(args[2]) : 100f);
    }

    static RealTerrainRoG ParseRog(JsonElement s)
    {
        var p = new RealTerrainRoG {
            CellsWide = s.GetProperty("nx").GetInt32(),
            CellsTall = s.GetProperty("ny").GetInt32(),
            CellSize = s.GetProperty("cell_size").GetDouble(),
            NValue = (float)s.GetProperty("manning_n").GetDouble(),
            Slope = 0.0,
            SolveDt = s.GetProperty("dt_s").GetDouble(),
            SolveDuration = s.GetProperty("sim_seconds").GetDouble(),
            ReportFrequency = s.GetProperty("report_every").GetInt32(),
            OutletEdge = s.GetProperty("outlet_edge").GetString(),
            OutletSlope = s.GetProperty("outlet_slope").GetDouble(),
            OutletStage = s.GetProperty("outlet_stage").GetDouble(),
            OutletBc = s.GetProperty("outlet_bc").GetString(),
            Diffusion = s.GetProperty("diffusion").GetBoolean(),
        };
        if (s.TryGetProperty("refine_dir", out var rd) && rd.ValueKind == JsonValueKind.String)
            p.RefineDir = rd.GetString();
        return p;
    }

    // -- fast mesh verification: build the (refined) mesh, dump cell centers +
    // counts, NO prepare/solve. The host computes the realized cell-size histogram
    // (nearest-neighbour cell-center spacing) to confirm the refinement landed. ------
    static int MeshProbe(string specPath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(specPath));
        var p = ParseRog(doc.RootElement);
        string outDir = doc.RootElement.GetProperty("out_dir").GetString();
        Directory.CreateDirectory(outDir);
        Mesh mesh = p.CreateMesh();
        var cc = mesh.CellCenters;
        using (var bw = new BinaryWriter(File.Open(Path.Combine(outDir, "cellcenters.f64"), FileMode.Create)))
            for (int i = 0; i < cc.Length; i++) { bw.Write(cc[i].X); bw.Write(cc[i].Y); }
        File.WriteAllText(Path.Combine(outDir, "mesh_probe.json"),
            $"{{\"cells\":{mesh.CellCount},\"faces\":{mesh.FaceCount},\"refined\":{(p.RefineDir != null ? "true" : "false")}}}");
        Console.WriteLine($"[meshprobe] cells={mesh.CellCount} faces={mesh.FaceCount} -> {outDir}");
        return 0;
    }

    // -- calibration / mass-check: closed flat basin, uniform precip -------------
    static int Rain(string outDir, float rate)
    {
        Console.WriteLine("[drv] rain basin dir=" + outDir + " rate=" + rate);
        var p = new RainBox {
            CellsTall = 15, CellsWide = 3, CellSize = 10.0, RampSeconds = 0.0,
            NValue = 0.03f, Slope = 0.0, DownstreamStage = 1.0, UpstreamFlow = 0.0,
            SolveDt = 2.0, SolveDuration = 3600.0, ReportFrequency = 30
        };
        AuthorWithPrecip(p, outDir, rate);
        return 0;
    }

    // -- real catchment rain-on-grid --------------------------------------------
    static int RealRog(string specPath)
    {
        using var doc = JsonDocument.Parse(File.ReadAllText(specPath));
        var s = doc.RootElement;
        string outDir = s.GetProperty("out_dir").GetString();
        var p = ParseRog(s);
        float rate = (float)s.GetProperty("precip_mm_hr").GetDouble();
        Console.WriteLine($"[drv] realrog dir={outDir} grid={p.CellsWide}x{p.CellsTall} cell={p.CellSize}m " +
                          $"rate={rate}mm/hr outlet={p.OutletEdge} dt={p.SolveDt} dur={p.SolveDuration} " +
                          $"refine={(p.RefineDir != null ? p.RefineDir : "off")}");
        AuthorWithPrecip(p, outDir, rate);
        return 0;
    }

    // -- ADR 0249 2D hydraulic-structure authoring (weir across a 2D channel) -----
    // A discriminating A/B: the SAME inflow channel WITH vs WITHOUT an internal weir.
    // The weir is authored as a Ras.Hydraulics.Structure (centerline Polyline crossing
    // the flow path + a StationElevation crest) added to the geometry's StructureLayer.
    // The engine derives the structure-to-cell/face pairing itself at prepare time
    // (HydraulicStructureCollection.IdentifyStructureCellsAndFaces) -- the caller
    // supplies NO pairing tables. Reuses the proven Save() terrain roundtrip: Save()
    // exports the terrain + writes geometry/BC/plans (throwing the known terrain-dir
    // bug before NValue, caught), then the project is re-opened, the weir injected into
    // the on-disk geometry, geom.Save() persists /Geometry/Structures, and the NValue
    // layer is written into Surface Layers (mirrors AuthorWithPrecip).
    static int StructDemo(string outDir, bool withWeir, double crestElev)
    {
        var p = new StructChannel {
            CellsWide = 6, CellsTall = 30, CellSize = 10.0,
            RampSeconds = 300.0, NValue = 0.03f, Slope = 0.0,
            DownstreamStage = 1.0, UpstreamFlow = 120.0,
            SolveDt = 2.0, SolveDuration = 6000.0, ReportFrequency = 20
        };
        double W = p.CellsWide * p.CellSize;
        double H = p.CellsTall * p.CellSize;
        Console.WriteLine($"[drv] structdemo dir={outDir} weir={withWeir} crest={crestElev} " +
                          $"basin={W}x{H}m inflow={p.UpstreamFlow} tailwater={p.DownstreamStage}");

        try { SyntheticTestCases.Save(p, outDir); Console.WriteLine("[drv] Save() completed"); }
        catch (Exception e) { Console.WriteLine("[drv] Save() aborted (known terrain bug): " + e.Message); }

        string ras = Directory.GetFiles(outDir, "*.ras").First();
        var project = new Project(ras);
        var geom = project.Geometries.First();
        var area = geom.FlowAreaLayer.First();
        string areaName = area.Name;
        Console.WriteLine($"[drv] reopened {Path.GetFileName(ras)} area='{areaName}' " +
                          $"cells={area.Mesh?.CellCount} geomfile={Path.GetFileName(geom.Filename)}");

        if (withWeir)
        {
            double ymid = H / 2.0;
            var pl = new Polyline(new List<Point> { new Point(0.0, ymid), new Point(W, ymid) });
            var crest = new StationElevationProfile(
                new double[] { 0.0, W }, new double[] { crestElev, crestElev });
            var st = new Structure {
                Polyline = pl,
                StationElevation = crest,
                WeirWidth = 3.0f,
                UpstreamSlope = 1.0f,
                DownstreamSlope = 1.0f,
                LWVelocityInto2D = false,
            };
            st.ID.Type = StructureType.Connection;
            st.ID.ConnectionName = "Weir1";
            st.UpstreamConnection = new StructureConnection {
                Type = StructureConnectionType.FlowArea, ConnectedElementName = areaName };
            st.DownstreamConnection = new StructureConnection {
                Type = StructureConnectionType.FlowArea, ConnectedElementName = areaName };
            geom.StructureLayer.Add(st);
            geom.Save();
            Console.WriteLine($"[drv] weir added: crest={crestElev} across y={ymid}, " +
                              $"structures={geom.StructureLayer.Count}, saved {geom.Filename}");
        }
        else
        {
            Console.WriteLine("[drv] baseline (no structure)");
        }

        var proj2 = SyntheticTestCases.CreateSyntheticTestCase(p, out _, out _, out _, out _);
        var surf = Path.Combine(outDir, "Surface Layers");
        Directory.CreateDirectory(surf);
        foreach (NValueLayer nv in proj2.NValues) nv.SaveAs(Path.Combine(surf, nv.Name + ".h5"));
        Console.WriteLine("[drv] structdemo done");
        return 0;
    }

    // The proven ADR 0207 sequence: Save() writes the project (terrain roundtrip +
    // associations), then set the constant precip on the BC + (re)write the NValue
    // layers. Save() throws the known Terrain.ExportFullCopy dir bug AFTER writing
    // .ras/Geometries/Terrains/BC/Plans but before NValue -- caught here.
    static void AuthorWithPrecip(ISyntheticTestCaseParams p, string outDir, float rate)
    {
        try { SyntheticTestCases.Save(p, outDir); Console.WriteLine("[drv] Save() completed"); }
        catch (Exception e) { Console.WriteLine("[drv] Save() aborted (known terrain bug): " + e.Message); }

        var project = SyntheticTestCases.CreateSyntheticTestCase(p, out _, out _, out _, out _);
        var bc = project.BoundaryConditions.First();
        var pr = bc.Precipitation;
        pr.IsEnabled = true;
        pr.SpatialDataType = SpatialDataType.Constant;
        pr.ConstantValue = rate;
        Console.WriteLine("[drv] precip const=" + pr.ConstantValue + " enabled=" + pr.IsEnabled +
                          " reallyEnabled=" + pr.IsReallyEnabledForCompute());
        string bcf = Path.Combine(outDir, "Boundary Conditions", bc.Name + ".h5");
        if (File.Exists(bcf)) File.Delete(bcf);
        bc.SaveAs(bcf);
        Console.WriteLine("[drv] wrote BC with precip: " + bcf);

        var surf = Path.Combine(outDir, "Surface Layers");
        Directory.CreateDirectory(surf);
        foreach (NValueLayer nv in project.NValues) nv.SaveAs(Path.Combine(surf, nv.Name + ".h5"));
        Console.WriteLine("[drv] done");
    }
}
