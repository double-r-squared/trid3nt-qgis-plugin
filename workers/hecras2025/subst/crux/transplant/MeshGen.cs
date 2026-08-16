using System;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Geospatial.Vectors;

// MESH REGENERATION (transplant experiment, terrain-independent): can the 2025
// beta REGENERATE the Muncie real-AOI 2D mesh from its RASMapper seed inputs
// (perimeter polygon + cell-center seed points)? MeshFactory.TryCreateMesh is
// the RASMapper mesh-generation entrypoint. Fidelity is judged by cell/face
// counts + how closely the regenerated cell centers match the shipped 6.x mesh.
// This answers "does 2025 author a real-AOI mesh (not just a FromExtent grid)?"
class MeshGen
{
    static Point[] ReadPts(string path)
    {
        var bytes = File.ReadAllBytes(path);
        int n = bytes.Length / 16;
        var pts = new Point[n];
        for (int i = 0; i < n; i++)
        {
            double x = BitConverter.ToDouble(bytes, i*16);
            double y = BitConverter.ToDouble(bytes, i*16 + 8);
            pts[i] = new Point(x, y);
        }
        return pts;
    }

    static int Main(string[] args)
    {
        string dir = args.Length > 0 ? args[0] : "/in";
        bool virtualCells = args.Length > 1 && args[1] == "virt";
        string perimFile = args.Length > 2 ? args[2] : "perimeter.f64";
        var perimPts = ReadPts(Path.Combine(dir, perimFile));
        Console.WriteLine("[meshgen] perimeter file = " + perimFile);
        var centerPts = ReadPts(Path.Combine(dir, "centers.f64"));
        Console.WriteLine($"[meshgen] perimeter pts={perimPts.Length}  cell-center seeds={centerPts.Length}");

        var perimeter = new Polygon(perimPts);
        var centers = new List<Point>(centerPts);

        // Build an empty breaklines list of the exact element type the API wants.
        var mi = typeof(MeshFactory).GetMethod("TryCreateMesh", BindingFlags.Public|BindingFlags.Static);
        var ps = mi.GetParameters();
        Type breakElem = ps[2].ParameterType.GetGenericArguments()[0];
        Console.WriteLine("[meshgen] breakline element type = " + breakElem.FullName);
        var emptyBreaks = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(breakElem));

        var mgp = MeshGenerationParams.Default();
        // characterize both options
        try { mgp.CreateVirtualCells = virtualCells; } catch {}
        try { mgp.SplitExternalFaces = true; } catch {}
        Console.WriteLine($"[meshgen] MeshGenerationParams CreateVirtualCells={virtualCells} SplitExternalFaces=true");

        // TryCreateMesh(perimeter, cellCenters, breaklines, out mesh, out error, mgp, reporter)
        object[] callArgs = new object[] { perimeter, centers, emptyBreaks, null, null, mgp, null };
        Console.WriteLine("[meshgen] >>> CALLING MeshFactory.TryCreateMesh <<<");
        bool ok;
        try { ok = (bool)mi.Invoke(null, callArgs); }
        catch (TargetInvocationException tie) { Console.WriteLine("[meshgen] EXCEPTION: " + tie.InnerException); return 2; }

        var mesh = callArgs[3] as Mesh;
        var err  = callArgs[4] as MeshError;
        Console.WriteLine("[meshgen] TryCreateMesh returned " + ok);
        if (err != null)
        {
            Console.WriteLine("[meshgen] MeshError.Status=" + err.Status + " fatalPerim=" + err.HasFatalPerimeterError);
            var msg = err.GetMeshErrorMessage();
            if (!string.IsNullOrEmpty(msg)) Console.WriteLine("[meshgen] MeshError.msg=" + msg);
        }
        if (mesh == null) { Console.WriteLine("[meshgen] mesh == null"); return 1; }

        Console.WriteLine($"[meshgen] REGENERATED mesh: cells={mesh.CellCount} faces={mesh.FaceCount} facepoints={mesh.FacePointCount}");
        Console.WriteLine($"[meshgen] 6.x reference:    cells=5765(5391 real) faces=11164 facepoints=5774");

        // Dump regenerated cell centers for a fidelity comparison against 6.x FIRST
        // (so a virtual-cells SanityCheck quirk does not lose the artifact).
        var cc = mesh.CellCenters;
        string outName = virtualCells ? "regen_centers_virt.f64" : "regen_centers.f64";
        using (var bw = new BinaryWriter(File.Open(Path.Combine(dir, outName), FileMode.Create)))
            foreach (var p in cc) { bw.Write(p.X); bw.Write(p.Y); }
        Console.WriteLine($"[meshgen] wrote {outName} ({cc.Length} centers)");

        try { var san = mesh.SanityCheck(false); Console.WriteLine("[meshgen] SanityCheck.Result=" + san.Result); }
        catch (Exception e) { Console.WriteLine("[meshgen] SanityCheck threw (non-fatal): " + e.GetType().Name + ": " + e.Message); }
        return 0;
    }
}
