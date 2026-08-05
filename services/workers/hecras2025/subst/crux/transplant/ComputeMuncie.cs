using System;
using System.IO;
using System.Collections;
using System.Collections.Generic;
using System.Reflection;
using Ras.Layers;
using Geospatial.Vectors;
using Geospatial.Vectors.Meshes;
using Geospatial.GDALAssist;

// Q2 NUMERIC-FIDELITY HARNESS (ADR 0132 close-out). Author the Muncie subgrid
// property tables via the 2025 beta path -- the REAL Muncie terrain + the
// bit-identical regenerated mesh (MeshFactory.TryCreateMesh from the shipped
// perimeter + 5391 cell-center seeds) + constant n=0.06 + the recorded
// PropertyTableOptions -- and dump the computed tables to raw binary for an
// element-wise A/B against the shipped 6.x GUI-computed tables. No server code.
class ComputeMuncie
{
    static Point[] ReadPts(string path)
    {
        var bytes = File.ReadAllBytes(path);
        int n = bytes.Length / 16;
        var pts = new Point[n];
        for (int i = 0; i < n; i++)
            pts[i] = new Point(BitConverter.ToDouble(bytes, i*16), BitConverter.ToDouble(bytes, i*16+8));
        return pts;
    }

    static void WriteF64(string path, IEnumerable<double> vals)
    {
        using var bw = new BinaryWriter(File.Open(path, FileMode.Create));
        foreach (var v in vals) bw.Write(v);
    }
    static void WriteF32(string path, float[] vals)
    {
        using var bw = new BinaryWriter(File.Open(path, FileMode.Create));
        foreach (var v in vals) bw.Write(v);
    }
    static void WriteI32(string path, IEnumerable<int> vals)
    {
        using var bw = new BinaryWriter(File.Open(path, FileMode.Create));
        foreach (var v in vals) bw.Write(v);
    }

    static int Main(string[] args)
    {
        string terrainPath = args.Length > 0 ? args[0] : "/work/Terrain.hdf";
        string nvPath      = args.Length > 1 ? args[1] : "/work/nvalue.h5";
        string inDir       = args.Length > 2 ? args[2] : "/in";
        string outDir      = args.Length > 3 ? args[3] : "/out";
        Directory.CreateDirectory(outDir);

        Console.WriteLine("[q2] Initializing GDAL...");
        GDALSetup.InitializeMultiplatform();
        Console.WriteLine("[q2] GDAL initialized OK");

        Console.WriteLine("[q2] Loading REAL Muncie elevation terrain: " + terrainPath);
        var terrain = new Terrain(terrainPath, null);
        var ext = terrain.ValidExtent;
        Console.WriteLine("[q2] Terrain valid extent: " + ext.ToString());
        Console.WriteLine("[q2] Loading nvalue terrain: " + nvPath);
        var nv = new Terrain(nvPath, null);

        // Regenerate the Muncie mesh from the shipped perimeter (CCW, open) + seeds.
        var perimPts  = ReadPts(Path.Combine(inDir, "perimeter_ccw_open.f64"));
        var centerPts = ReadPts(Path.Combine(inDir, "centers.f64"));
        Console.WriteLine($"[q2] perimeter pts={perimPts.Length}  cell-center seeds={centerPts.Length}");
        var perimeter = new Polygon(perimPts);
        var centers = new List<Point>(centerPts);

        var mi = typeof(MeshFactory).GetMethod("TryCreateMesh", BindingFlags.Public|BindingFlags.Static);
        Type breakElem = mi.GetParameters()[2].ParameterType.GetGenericArguments()[0];
        var emptyBreaks = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(breakElem));
        var mgp = MeshGenerationParams.Default();
        try { mgp.CreateVirtualCells = false; } catch {}
        try { mgp.SplitExternalFaces = true; } catch {}
        object[] callArgs = new object[] { perimeter, centers, emptyBreaks, null, null, mgp, null };
        Console.WriteLine("[q2] >>> MeshFactory.TryCreateMesh <<<");
        bool ok = (bool)mi.Invoke(null, callArgs);
        var mesh = callArgs[3] as Mesh;
        var merr = callArgs[4] as MeshError;
        Console.WriteLine($"[q2] TryCreateMesh={ok} status={merr?.Status} cells={mesh.CellCount} faces={mesh.FaceCount} facepoints={mesh.FacePointCount}");

        // Probe: does the REAL terrain resample interior cell centers to real elevations?
        try {
            var cc0 = mesh.CellCenters;
            var probe = new System.Memory<Point>(new Point[]{ cc0[0], cc0[cc0.Length/2], cc0[cc0.Length-1] });
            var pv = new float[3]; terrain.SamplePoints(probe, pv);
            Console.WriteLine("[q2] terrain.SamplePoints(cell centers) = [" + string.Join(", ", pv) + "]");
            var nvv = new float[3]; nv.SamplePoints(probe, nvv);
            Console.WriteLine("[q2] nvalue.SamplePoints = [" + string.Join(", ", nvv) + "]");
        } catch (Exception e) { Console.WriteLine("[q2] probe err: " + e.GetType().Name + ": " + e.Message); }

        // The recorded Muncie PropertyTableOptions (geometry Attributes, ADR 0132):
        // Cell Vol Tol 0.01, Face Conv Ratio 0.02, Face Profile/Area Tol 0.01,
        // Cell Min Area Fraction 0.01, Laminar Depth 0.2.
        var opts = new PropertyTableOptions
        {
            CellVolumeFilterTolerance   = 0.01,
            FaceAreaConveyanceRatio     = 0.02,
            CellMinAreaFraction         = 0.01,
            FaceProfileFilterTolerance  = 0.01,
            FaceAreaElevFilterTolerance = 0.01,
            FaceLaminarDepthTolerance   = 0.2,
        };
        Console.WriteLine("[q2] opts SampledPointsPerCell=" + opts.SampledPointsPerCell +
                          " BinsPerCell=" + opts.BinsPerCell + " SampledPointsPerFace=" + opts.SampledPointsPerFace);

        Console.WriteLine("[q2] >>> MeshPropertyTables.ComputeFrom (REAL terrain) <<<");
        MeshPropertyTables tables;
        var res = MeshPropertyTables.ComputeFrom(mesh, terrain, nv, opts, out tables, null);
        Console.WriteLine("[q2] ComputeFrom Result=" + res.Result);
        int nmsg = 0;
        foreach (var m in res.Messages) { if (nmsg++ < 20) Console.WriteLine("[q2]   msg: " + m.EvaluatedMessage); }
        if (nmsg > 20) Console.WriteLine($"[q2]   ... {nmsg-20} more messages");
        if (tables == null) { Console.WriteLine("[q2] tables == null -- ABORT"); return 1; }

        // ---- dump geometry (for matching to 6.x by center / midpoint) ----
        int nc = mesh.CellCount, nf = mesh.FaceCount;
        var cc = mesh.CellCenters;
        var cellXY = new double[nc*2];
        for (int i=0;i<nc;i++){ cellXY[i*2]=cc[i].X; cellXY[i*2+1]=cc[i].Y; }
        WriteF64(Path.Combine(outDir,"regen_cell_centers.f64"), cellXY);
        var faceXY = new double[nf*2];
        for (int i=0;i<nf;i++){ var mp = mesh.FaceMidPoint(i); faceXY[i*2]=mp.X; faceXY[i*2+1]=mp.Y; }
        WriteF64(Path.Combine(outDir,"regen_face_midpoints.f64"), faceXY);

        // ---- dump the computed property tables (ragged Info + flat Values) ----
        var cellInfo = new int[nc*2];
        for (int i=0;i<nc;i++){ cellInfo[i*2]=tables.CellTableStart[i]; cellInfo[i*2+1]=tables.CellTableCount[i]; }
        WriteI32(Path.Combine(outDir,"cell_info.i32"), cellInfo);
        WriteF32(Path.Combine(outDir,"cell_elev.f32"), tables.CellElevationTable);
        WriteF32(Path.Combine(outDir,"cell_vol.f32"),  tables.CellVolumeTable);
        if (tables.CellWettedAreaTable != null) WriteF32(Path.Combine(outDir,"cell_wetarea.f32"), tables.CellWettedAreaTable);

        var faceInfo = new int[nf*2];
        for (int i=0;i<nf;i++){ faceInfo[i*2]=tables.FaceTableStart[i]; faceInfo[i*2+1]=tables.FaceTableCount[i]; }
        WriteI32(Path.Combine(outDir,"face_info.i32"), faceInfo);
        WriteF32(Path.Combine(outDir,"face_elev.f32"), tables.FaceElevationTable);
        WriteF32(Path.Combine(outDir,"face_area.f32"), tables.FaceAreaTable);
        WriteF32(Path.Combine(outDir,"face_wp.f32"),   tables.FaceWettedPerimeterTable);
        WriteF32(Path.Combine(outDir,"face_mann.f32"), tables.FaceManningsTable);

        Console.WriteLine($"[q2] DUMPED tables: cells={nc} (vol.len={tables.CellVolumeTable.Length}) " +
                          $"faces={nf} (area.len={tables.FaceAreaTable.Length}) -> {outDir}");
        // sanity: cell0 + face0 first rows
        int cs=tables.CellTableStart[0], cn=tables.CellTableCount[0];
        Console.Write("[q2] cell0 (elev,vol):");
        for (int i=cs;i<cs+Math.Min(cn,4);i++) Console.Write($" ({tables.CellElevationTable[i]:F3},{tables.CellVolumeTable[i]:F3})");
        Console.WriteLine();
        int fs=tables.FaceTableStart[0], fn=tables.FaceTableCount[0];
        Console.Write("[q2] face0 (elev,area,wp,mann):");
        for (int i=fs;i<fs+Math.Min(fn,4);i++) Console.Write($" ({tables.FaceElevationTable[i]:F3},{tables.FaceAreaTable[i]:F3},{tables.FaceWettedPerimeterTable[i]:F3},{tables.FaceManningsTable[i]:F3})");
        Console.WriteLine();
        Console.WriteLine("[q2] DONE Result=" + res.Result);
        return res.Result ? 0 : 2;
    }
}
