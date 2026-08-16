using System;
using System.IO;
using System.Reflection;
using System.Collections;
using System.Collections.Generic;
using Ras.Layers;
using Geospatial.Vectors;
using Geospatial.Vectors.Meshes;
using Geospatial.GDALAssist;

// AUTHOR-MESH (OI-B, ADR 0133/0134 link c1 -- the general full-topology dump).
//
// Generalizes ComputeMuncie.cs (ADR 0132, which dumped only cell centers + face
// midpoints + subgrid tables for the Q2 A/B) into the AUTHORING-worker stage: from
// a perimeter polygon + cell-center seeds, regenerate a mesh via
// MeshFactory.TryCreateMesh, optionally compute the subgrid property tables via
// MeshPropertyTables.ComputeFrom over a real terrain, and dump the FULL mesh
// TOPOLOGY -- every array the Python hecras_geometry_writer.Mesh2D needs -- so a
// genuinely-new AOI mesh can be serialized into the 6.x /Geometry/2D Flow Areas/
// schema (the ADR 0134 c1 spec).
//
// The 2025 beta Mesh exposes (ApiProbe, api_probe.txt):
//   Face:      public int cellA, cellB, fpA, fpB        (Faces Cell/FacePoint Indexes)
//   FacePoint: public Point Point; IList<int> Faces     (coords + face adjacency)
//   Cell:      public Memory<int> Faces                 (per-cell face list)
//   Mesh:      Point[] CellCenters; Polygon Perimeter;
//              CellIsVirtual(i)/CellIsPerimeter(i)/FacePointIsPerimeter(i);
//              Vector[] ComputeFaceNormals()
// so the whole topology is a direct field read -- the Python adapter derives the
// HEC ragged/orientation conventions from these primitives (as carve_muncie does).
//
// Terrain is OPTIONAL: with no terrain arg the dump is topology-only (the c1 dump +
// its terrain-free structural validation vs the shipped Muncie mesh); with a terrain
// + nvalue the subgrid tables are computed + dumped too (the full authoring path).
// No server code; reflection where the beta API is generic.
class AuthorMesh
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
    { using var bw = new BinaryWriter(File.Open(path, FileMode.Create)); foreach (var v in vals) bw.Write(v); }
    static void WriteF32(string path, IEnumerable<float> vals)
    { using var bw = new BinaryWriter(File.Open(path, FileMode.Create)); foreach (var v in vals) bw.Write(v); }
    static void WriteI32(string path, IEnumerable<int> vals)
    { using var bw = new BinaryWriter(File.Open(path, FileMode.Create)); foreach (var v in vals) bw.Write(v); }

    static int Main(string[] args)
    {
        // Args: <inDir> <outDir> [terrain.hdf] [nvalue.h5]
        //   inDir holds perimeter_ccw_open.f64 + centers.f64 (the RASMapper seeds).
        string inDir  = args.Length > 0 ? args[0] : "/in";
        string outDir = args.Length > 1 ? args[1] : "/out";
        string terrainPath = args.Length > 2 ? args[2] : null;
        string nvPath      = args.Length > 3 ? args[3] : null;
        Directory.CreateDirectory(outDir);

        Console.WriteLine("[author] Initializing GDAL...");
        GDALSetup.InitializeMultiplatform();

        var perimPts  = ReadPts(Path.Combine(inDir, "perimeter_ccw_open.f64"));
        var centerPts = ReadPts(Path.Combine(inDir, "centers.f64"));
        Console.WriteLine($"[author] perimeter pts={perimPts.Length}  cell-center seeds={centerPts.Length}");
        var perimeter = new Polygon(perimPts);
        var centers = new List<Point>(centerPts);

        var mi = typeof(MeshFactory).GetMethod("TryCreateMesh", BindingFlags.Public|BindingFlags.Static);
        Type breakElem = mi.GetParameters()[2].ParameterType.GetGenericArguments()[0];
        var emptyBreaks = (IList)Activator.CreateInstance(typeof(List<>).MakeGenericType(breakElem));
        var mgp = MeshGenerationParams.Default();
        try { mgp.CreateVirtualCells = false; } catch {}
        try { mgp.SplitExternalFaces = true; } catch {}
        object[] callArgs = new object[] { perimeter, centers, emptyBreaks, null, null, mgp, null };
        Console.WriteLine("[author] >>> MeshFactory.TryCreateMesh <<<");
        bool ok = (bool)mi.Invoke(null, callArgs);
        var mesh = callArgs[3] as Mesh;
        var merr = callArgs[4] as MeshError;
        if (mesh == null) { Console.WriteLine("[author] mesh == null -- ABORT"); return 1; }
        int nc = mesh.CellCount, nf = mesh.FaceCount, nfp = mesh.FacePointCount;
        Console.WriteLine($"[author] TryCreateMesh={ok} status={merr?.Status} cells={nc} faces={nf} facepoints={nfp}");

        // ---- FULL TOPOLOGY DUMP (the c1 arrays; direct field reads) ----
        // Faces: [cellA, cellB, fpA, fpB] per face.
        var faces = new int[nf*4];
        for (int i=0;i<nf;i++){ var fe = mesh.Faces[i]; faces[i*4]=fe.cellA; faces[i*4+1]=fe.cellB; faces[i*4+2]=fe.fpA; faces[i*4+3]=fe.fpB; }
        WriteI32(Path.Combine(outDir,"faces.i32"), faces);

        // FacePoints: coordinates.
        var fpXY = new double[nfp*2];
        for (int i=0;i<nfp;i++){ var p = mesh.FacePoints[i].Point; fpXY[i*2]=p.X; fpXY[i*2+1]=p.Y; }
        WriteF64(Path.Combine(outDir,"facepoints.f64"), fpXY);

        // Cell centers.
        var cc = mesh.CellCenters;
        var ccXY = new double[nc*2];
        for (int i=0;i<nc;i++){ ccXY[i*2]=cc[i].X; ccXY[i*2+1]=cc[i].Y; }
        WriteF64(Path.Combine(outDir,"cellcenters.f64"), ccXY);

        // Per-cell face list (ragged Info[start,count] + flat values).
        var cellFaceInfo = new int[nc*2];
        var cellFaceVals = new List<int>();
        for (int i=0;i<nc;i++){
            var mem = mesh.Cells[i].Faces;          // Memory<int>
            var sp = mem.Span;
            cellFaceInfo[i*2]=cellFaceVals.Count; cellFaceInfo[i*2+1]=sp.Length;
            for (int k=0;k<sp.Length;k++) cellFaceVals.Add(sp[k]);
        }
        WriteI32(Path.Combine(outDir,"cell_face_info.i32"), cellFaceInfo);
        WriteI32(Path.Combine(outDir,"cell_face_vals.i32"), cellFaceVals);

        // Per-facepoint face adjacency (ragged) -- FacePoint.Faces is IList<int>.
        var fpFaceInfo = new int[nfp*2];
        var fpFaceVals = new List<int>();
        for (int i=0;i<nfp;i++){
            var fl = mesh.FacePoints[i].Faces;       // IList<int>
            fpFaceInfo[i*2]=fpFaceVals.Count; fpFaceInfo[i*2+1]=fl.Count;
            for (int k=0;k<fl.Count;k++) fpFaceVals.Add(fl[k]);
        }
        WriteI32(Path.Combine(outDir,"fp_face_info.i32"), fpFaceInfo);
        WriteI32(Path.Combine(outDir,"fp_face_vals.i32"), fpFaceVals);

        // Flags: virtual/perimeter markers.
        var cellVirt = new int[nc]; var cellPer = new int[nc];
        for (int i=0;i<nc;i++){ cellVirt[i]=mesh.CellIsVirtual(i)?1:0; cellPer[i]=mesh.CellIsPerimeter(i)?1:0; }
        WriteI32(Path.Combine(outDir,"cell_isvirtual.i32"), cellVirt);
        WriteI32(Path.Combine(outDir,"cell_isperimeter.i32"), cellPer);
        var fpPer = new int[nfp];
        for (int i=0;i<nfp;i++) fpPer[i]=mesh.FacePointIsPerimeter(i)?1:0;
        WriteI32(Path.Combine(outDir,"fp_isperimeter.i32"), fpPer);

        // Face normals (Vector[]) -- for the writer's NormalUnitVector + a cross-check.
        try {
            var normals = mesh.ComputeFaceNormals();
            var nrm = new double[nf*2];
            for (int i=0;i<nf;i++){ var v = normals[i]; nrm[i*2]=GetD(v,"X"); nrm[i*2+1]=GetD(v,"Y"); }
            WriteF64(Path.Combine(outDir,"face_normals.f64"), nrm);
        } catch (Exception e) { Console.WriteLine("[author] normals dump skipped: " + e.GetType().Name); }

        // Perimeter polygon points.
        try {
            var perimPoly = mesh.Perimeter;
            var pp = GetPolygonPoints(perimPoly);
            var ppXY = new double[pp.Count*2];
            for (int i=0;i<pp.Count;i++){ ppXY[i*2]=pp[i].X; ppXY[i*2+1]=pp[i].Y; }
            WriteF64(Path.Combine(outDir,"perimeter.f64"), ppXY);
            Console.WriteLine($"[author] perimeter polygon pts={pp.Count}");
        } catch (Exception e) { Console.WriteLine("[author] perimeter dump skipped: " + e.GetType().Name + ": " + e.Message); }

        // meta
        File.WriteAllText(Path.Combine(outDir,"topo_meta.json"),
            $"{{\"cell_count\":{nc},\"face_count\":{nf},\"facepoint_count\":{nfp},\"ok\":{(ok?"true":"false")}}}");

        Console.WriteLine($"[author] TOPOLOGY DUMPED: cells={nc} faces={nf} facepoints={nfp} -> {outDir}");

        // ---- OPTIONAL: subgrid tables over real terrain (the full authoring path) ----
        if (terrainPath != null && nvPath != null) {
            Console.WriteLine("[author] Loading terrain: " + terrainPath);
            var terrain = new Terrain(terrainPath, null);
            var nv = new Terrain(nvPath, null);
            var opts = new PropertyTableOptions {
                CellVolumeFilterTolerance=0.01, FaceAreaConveyanceRatio=0.02,
                CellMinAreaFraction=0.01, FaceProfileFilterTolerance=0.01,
                FaceAreaElevFilterTolerance=0.01, FaceLaminarDepthTolerance=0.2 };
            Console.WriteLine("[author] >>> MeshPropertyTables.ComputeFrom <<<");
            MeshPropertyTables tables;
            var res = MeshPropertyTables.ComputeFrom(mesh, terrain, nv, opts, out tables, null);
            Console.WriteLine("[author] ComputeFrom Result=" + res.Result);
            if (tables != null) {
                var ci = new int[nc*2];
                for (int i=0;i<nc;i++){ ci[i*2]=tables.CellTableStart[i]; ci[i*2+1]=tables.CellTableCount[i]; }
                WriteI32(Path.Combine(outDir,"cell_info.i32"), ci);
                WriteF32(Path.Combine(outDir,"cell_elev.f32"), tables.CellElevationTable);
                WriteF32(Path.Combine(outDir,"cell_vol.f32"),  tables.CellVolumeTable);
                var fi = new int[nf*2];
                for (int i=0;i<nf;i++){ fi[i*2]=tables.FaceTableStart[i]; fi[i*2+1]=tables.FaceTableCount[i]; }
                WriteI32(Path.Combine(outDir,"face_info.i32"), fi);
                WriteF32(Path.Combine(outDir,"face_elev.f32"), tables.FaceElevationTable);
                WriteF32(Path.Combine(outDir,"face_area.f32"), tables.FaceAreaTable);
                WriteF32(Path.Combine(outDir,"face_wp.f32"),   tables.FaceWettedPerimeterTable);
                WriteF32(Path.Combine(outDir,"face_mann.f32"), tables.FaceManningsTable);
                Console.WriteLine("[author] SUBGRID TABLES DUMPED (cells+faces).");
            }
            return res.Result ? 0 : 2;
        }
        return 0;
    }

    // reflection helpers (the beta Vector/Polygon shapes vary by build)
    static double GetD(object o, string prop)
    { var p = o.GetType().GetProperty(prop); if (p!=null) return Convert.ToDouble(p.GetValue(o));
      var f = o.GetType().GetField(prop); return f!=null ? Convert.ToDouble(f.GetValue(o)) : 0.0; }

    static List<Point> GetPolygonPoints(Polygon poly)
    {
        // Polygon exposes its points via an enumerable/collection; find it reflectively.
        foreach (var pn in new[]{"Points","PointCollection","Vertices"}) {
            var pr = poly.GetType().GetProperty(pn);
            if (pr != null) {
                var val = pr.GetValue(poly);
                if (val is IEnumerable en) {
                    var outp = new List<Point>();
                    foreach (var it in en) if (it is Point pt) outp.Add(pt);
                    if (outp.Count > 0) return outp;
                }
            }
        }
        // Fallback: Polygon itself may be IEnumerable<Point>.
        if (poly is IEnumerable e2) { var outp = new List<Point>(); foreach (var it in e2) if (it is Point pt) outp.Add(pt); return outp; }
        return new List<Point>();
    }
}
