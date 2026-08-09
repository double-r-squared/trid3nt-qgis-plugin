using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using Ras.Synthetics;
using Ras.Layers;
using Ras.Layers.BoundaryConditions;
using Ras.Engine;
using Geospatial.Vectors;
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

class Driver
{
    static int Main(string[] args)
    {
        GDALSetup.InitializeMultiplatform();
        string mode = args.Length > 0 ? args[0] : "rain";
        if (mode == "realrog")
            return RealRog(args[1]);
        return Rain(args.Length > 1 ? args[1] : "/probe/rain",
                    args.Length > 2 ? float.Parse(args[2]) : 100f);
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
        float rate = (float)s.GetProperty("precip_mm_hr").GetDouble();
        Console.WriteLine($"[drv] realrog dir={outDir} grid={p.CellsWide}x{p.CellsTall} cell={p.CellSize}m " +
                          $"rate={rate}mm/hr outlet={p.OutletEdge} dt={p.SolveDt} dur={p.SolveDuration}");
        AuthorWithPrecip(p, outDir, rate);
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
