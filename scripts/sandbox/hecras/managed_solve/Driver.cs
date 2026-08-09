using System;
using System.IO;
using System.Linq;
using Ras.Synthetics;
using Ras.Layers;
using Ras.Layers.BoundaryConditions;
using Geospatial.Vectors;
using Geospatial.GDALAssist;

// Rain-only basin: no inflow/outflow BC lines; forcing is a uniform constant
// precipitation applied in-memory by PrecipitationLayer.InitializeComputeDriver.
class RainBox : InOutPlanarParams
{
    public override BoundaryConditionLine GetUpstreamBC(Ras.Layers.BoundaryCondition bc, Polyline pl) => null;
    public override BoundaryConditionLine GetDownstreamBC(Ras.Layers.BoundaryCondition bc, Polyline pl) => null;
}

class Driver
{
    static int Main(string[] args)
    {
        string outDir = args.Length > 0 ? args[0] : "/probe/rain";
        float rate = args.Length > 1 ? float.Parse(args[1]) : 100f;
        Console.WriteLine("[drv] GDAL init...");
        GDALSetup.InitializeMultiplatform();
        var p = new RainBox {
            CellsTall = 15, CellsWide = 3, CellSize = 10.0, RampSeconds = 0.0,
            NValue = 0.03f, Slope = 0.0, DownstreamStage = 1.0, UpstreamFlow = 0.0,
            SolveDt = 2.0, SolveDuration = 3600.0, ReportFrequency = 30
        };
        try { SyntheticTestCases.Save(p, outDir); Console.WriteLine("[drv] Save() completed"); }
        catch (Exception e) { Console.WriteLine("[drv] Save() aborted (known terrain bug): " + e.Message); }

        var project = SyntheticTestCases.CreateSyntheticTestCase(p, out _, out _, out _, out _);
        var bc = project.BoundaryConditions.First();
        var pr = bc.Precipitation;
        pr.IsEnabled = true;
        pr.SpatialDataType = SpatialDataType.Constant;
        pr.ConstantValue = rate;
        Console.WriteLine("[drv] precip: const=" + pr.ConstantValue + " enabled=" + pr.IsEnabled +
                          " reallyEnabled=" + pr.IsReallyEnabledForCompute());
        string bcf = Path.Combine(outDir, "Boundary Conditions", bc.Name + ".h5");
        if (File.Exists(bcf)) File.Delete(bcf);
        bc.SaveAs(bcf);
        Console.WriteLine("[drv] rewrote BC with precip: " + bcf);

        var surf = Path.Combine(outDir, "Surface Layers");
        Directory.CreateDirectory(surf);
        foreach (NValueLayer nv in project.NValues) nv.SaveAs(Path.Combine(surf, nv.Name + ".h5"));
        Console.WriteLine("[drv] done");
        return 0;
    }
}
