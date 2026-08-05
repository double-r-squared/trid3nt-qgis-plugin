using System;
using Ras.Layers;
using Geospatial.Vectors;
using Geospatial.Vectors.Meshes;
using Geospatial.GDALAssist;

// CRUX harness: directly exercise MeshPropertyTables.ComputeFrom (the subgrid
// property-table computation that `ras prepare` runs) under the substituted
// GDAL/HDF5 natives. Bypasses the .ras project parse wall. No server code.
class Harness
{
    static int Main(string[] args)
    {
        string terrainPath = args.Length > 0 ? args[0] : "/work/T2.h5";
        string nvPath      = args.Length > 1 ? args[1] : "/work/nvalue.h5";
        int cellsW         = args.Length > 2 ? int.Parse(args[2]) : 8;
        int cellsH         = args.Length > 3 ? int.Parse(args[3]) : 8;

        Console.WriteLine("[crux] Initializing GDAL...");
        GDALSetup.InitializeMultiplatform();
        Console.WriteLine("[crux] GDAL initialized OK");

        Console.WriteLine("[crux] Loading elevation terrain: " + terrainPath);
        var terrain = new Terrain(terrainPath, null);
        var ext = terrain.ValidExtent;
        Console.WriteLine("[crux] Terrain valid extent: " + ext.ToString());

        Console.WriteLine("[crux] Loading nvalue terrain: " + nvPath);
        var nv = new Terrain(nvPath, null);

        // Probe: does the terrain resampler return real elevations under substituted GDAL?
        try {
            var cx = (ext.MinX + ext.MaxX) / 2.0;
            var cy = (ext.MinY + ext.MaxY) / 2.0;
            var probePts = new System.Memory<Point>(new Point[] {
                new Point(cx, cy),
                new Point(ext.MinX + ext.Width*0.25, ext.MinY + ext.Height*0.25),
                new Point(ext.MinX + ext.Width*0.75, ext.MinY + ext.Height*0.75),
            });
            var pv = new float[probePts.Length];
            terrain.SamplePoints(probePts, pv);
            Console.WriteLine("[crux] terrain.SamplePoints probe: [" + string.Join(", ", pv) + "]");
            var nvv = new float[probePts.Length];
            nv.SamplePoints(probePts, nvv);
            Console.WriteLine("[crux] nvalue.SamplePoints probe: [" + string.Join(", ", nvv) + "]");
        } catch (Exception e) { Console.WriteLine("[crux] probe err: " + e.GetType().Name + ": " + e.Message); }

        Console.WriteLine("[crux] Building mesh from extent " + cellsW + "x" + cellsH);
        var mext = ext.RatioAroundCenter(0.80); // inset so perimeter faces sample interior terrain (avoid edge NoData)
        Console.WriteLine("[crux] Mesh (inset) extent: " + mext.ToString());
        var mesh = MeshFactory.FromExtent(mext, cellsW, cellsH);
        Console.WriteLine("[crux] Mesh cells=" + mesh.CellCount + " faces=" + mesh.FaceCount);

        var opts = new PropertyTableOptions
        {
            CellVolumeFilterTolerance   = 0.01,
            FaceAreaConveyanceRatio     = 0.02,
            CellMinAreaFraction         = 0.01,
            FaceProfileFilterTolerance  = 0.01,
            FaceAreaElevFilterTolerance = 0.01,
            FaceLaminarDepthTolerance   = 0.01,
        };
        Console.WriteLine("[crux] opts SampledPointsPerCell=" + opts.SampledPointsPerCell +
                          " BinsPerCell=" + opts.BinsPerCell +
                          " SampledPointsPerFace=" + opts.SampledPointsPerFace);

        Console.WriteLine("[crux] >>> CALLING MeshPropertyTables.ComputeFrom (the prepare subgrid step) <<<");
        MeshPropertyTables tables;
        var res = MeshPropertyTables.ComputeFrom(mesh, terrain, nv, opts, out tables, null);
        Console.WriteLine("[crux] ComputeFrom returned. Result=" + res.Result);
        foreach (var m in res.Messages)
            Console.WriteLine("[crux]   msg: " + m.EvaluatedMessage);

        if (tables != null)
        {
            Console.WriteLine("[crux] TABLES BUILT:");
            Console.WriteLine("[crux]   CellTableStart.len=" + (tables.CellTableStart?.Length ?? -1));
            Console.WriteLine("[crux]   CellTableCount.len=" + (tables.CellTableCount?.Length ?? -1));
            Console.WriteLine("[crux]   FaceTableStart.len=" + (tables.FaceTableStart?.Length ?? -1));
            Console.WriteLine("[crux]   FaceTableCount.len=" + (tables.FaceTableCount?.Length ?? -1));
            try { Console.WriteLine("[crux]   CellVolumeTable.len=" + tables.CellVolumeTable.Length); } catch (Exception e) { Console.WriteLine("[crux]   CellVolumeTable err: " + e.Message); }
            try { Console.WriteLine("[crux]   CellElevationTable.len=" + tables.CellElevationTable.Length); } catch (Exception e) { Console.WriteLine("[crux]   CellElevationTable err: " + e.Message); }
            try { Console.WriteLine("[crux]   FaceElevationTable.len=" + tables.FaceElevationTable.Length); } catch (Exception e) { Console.WriteLine("[crux]   FaceElevationTable err: " + e.Message); }
            try { Console.WriteLine("[crux]   FaceAreaTable.len=" + tables.FaceAreaTable.Length); } catch (Exception e) { Console.WriteLine("[crux]   FaceAreaTable err: " + e.Message); }
            // Sample a few subgrid property values for cell 0 to characterize the table content
            try {
                int s = tables.CellTableStart[0]; int c = tables.CellTableCount[0];
                Console.WriteLine("[crux]   cell0 table start=" + s + " count=" + c);
                var ve = tables.CellVolumeTable; var el = tables.CellElevationTable;
                for (int i = s; i < s + System.Math.Min((int)c, 6); i++)
                    Console.WriteLine("[crux]     cell0[" + i + "] vol=" + ve[i] + " elev=" + el[i]);
                int fs = tables.FaceTableStart[0]; int fc = tables.FaceTableCount[0];
                Console.WriteLine("[crux]   face0 table start=" + fs + " count=" + fc);
                var fel = tables.FaceElevationTable; var far = tables.FaceAreaTable;
                for (int i = fs; i < fs + System.Math.Min((int)fc, 6); i++)
                    Console.WriteLine("[crux]     face0[" + i + "] elev=" + fel[i] + " area=" + far[i]);
            } catch (Exception e) { Console.WriteLine("[crux]   value dump err: " + e.Message); }
        }
        else Console.WriteLine("[crux] tables == null");

        Console.WriteLine("[crux] VERDICT: property-table computation completed without EntryPointNotFound.");
        return 0;
    }
}
