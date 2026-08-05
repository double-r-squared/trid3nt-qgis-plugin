using System;
using System.Linq;
using System.Reflection;
using System.Collections.Generic;

// API PROBE (transplant experiment): reflect over the 2025 beta's Mesh /
// MeshFactory / Terrain / MeshPropertyTables / MeshReader surfaces to answer
// THE decisive scope question: can the 2025 path AUTHOR AN ARBITRARY (imported)
// mesh -- explicit cell points / facepoints / faces -- or only MeshFactory.FromExtent?
// This determines whether "2025 authors any mesh -> 6.x solves" is reachable for
// a real AOI, or whether the transplant is bounded to FromExtent-shaped meshes.
// No server code; reflection only.
class ApiProbe
{
    static void DumpType(Type t)
    {
        if (t == null) { Console.WriteLine("  (type not found)"); return; }
        Console.WriteLine("==== " + t.FullName + " (assembly " + t.Assembly.GetName().Name + ") ====");
        Console.WriteLine("  -- public constructors --");
        foreach (var c in t.GetConstructors(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance))
            Console.WriteLine("    " + (c.IsPublic?"pub ":"int ") + "ctor(" + string.Join(", ", c.GetParameters().Select(p=>p.ParameterType.Name+" "+p.Name)) + ")");
        Console.WriteLine("  -- static methods --");
        foreach (var m in t.GetMethods(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Static).OrderBy(m=>m.Name))
            if (!m.Name.StartsWith("get_") && !m.Name.StartsWith("set_"))
                Console.WriteLine("    " + (m.IsPublic?"pub ":"int ") + "static " + m.ReturnType.Name + " " + m.Name + "(" + string.Join(", ", m.GetParameters().Select(p=>p.ParameterType.Name+" "+p.Name)) + ")");
        Console.WriteLine("  -- instance methods (declared) --");
        foreach (var m in t.GetMethods(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance|BindingFlags.DeclaredOnly).OrderBy(m=>m.Name))
            if (!m.Name.StartsWith("get_") && !m.Name.StartsWith("set_"))
                Console.WriteLine("    " + (m.IsPublic?"pub ":"int ") + m.ReturnType.Name + " " + m.Name + "(" + string.Join(", ", m.GetParameters().Select(p=>p.ParameterType.Name+" "+p.Name)) + ")");
        Console.WriteLine("  -- public/settable properties + fields --");
        foreach (var p in t.GetProperties(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance).OrderBy(p=>p.Name))
            Console.WriteLine("    prop " + (p.CanWrite?"RW ":"RO ") + p.PropertyType.Name + " " + p.Name);
        foreach (var fld in t.GetFields(BindingFlags.Public|BindingFlags.Instance).OrderBy(p=>p.Name))
            Console.WriteLine("    field " + fld.FieldType.Name + " " + fld.Name);
    }

    static int Main(string[] args)
    {
        // Force-load the assemblies via a known type.
        var asmGeo = typeof(Geospatial.Vectors.Meshes.MeshPropertyTables).Assembly;
        var asmRas = typeof(Ras.Layers.Terrain).Assembly;
        Console.WriteLine("[probe] Geospatial asm: " + asmGeo.FullName);
        Console.WriteLine("[probe] Ras asm: " + asmRas.FullName);

        // Enumerate every Mesh-related type in the Geospatial vectors/meshes namespaces.
        Console.WriteLine("\n[probe] ==== ALL TYPES in Geospatial.Vectors.Meshes ====");
        foreach (var t in asmGeo.GetTypes().Where(t=>t.Namespace!=null && t.Namespace.Contains("Meshes")).OrderBy(t=>t.Name))
            Console.WriteLine("  " + t.FullName + (t.IsAbstract?" [abstract]":"") + (t.IsInterface?" [iface]":""));

        // Resolve the REAL Mesh type from ComputeFrom's first parameter (namespace-agnostic).
        var computeFrom = typeof(Geospatial.Vectors.Meshes.MeshPropertyTables)
            .GetMethod("ComputeFrom", BindingFlags.Public|BindingFlags.Static);
        Type meshType = computeFrom.GetParameters()[0].ParameterType;
        Console.WriteLine("\n[probe] Mesh param type = " + meshType.FullName + " in " + meshType.Assembly.GetName().Name);

        // Find all types named Mesh / MeshFactory / MeshReader / MeshWriter across loaded assemblies.
        var allAsm = AppDomain.CurrentDomain.GetAssemblies();
        foreach (var wanted in new[]{"Mesh","MeshFactory","MeshReader","MeshWriter","VectorMesh","IResample`1","Terrain"})
        {
            foreach (var asm in allAsm)
            {
                Type[] ts; try { ts = asm.GetTypes(); } catch { continue; }
                foreach (var t in ts)
                    if (t.Name == wanted) Console.WriteLine("[probe] FOUND '" + wanted + "' => " + t.FullName + " (" + asm.GetName().Name + ")");
            }
        }

        var dumpTargets = new List<Type> { meshType,
            typeof(Geospatial.Vectors.Meshes.MeshPropertyTables),
            typeof(Geospatial.Vectors.Meshes.PropertyTableOptions) };
        foreach (var wanted in new[]{"MeshFactory","MeshReader","MeshWriter",
                 "Polygon","Point","PointCollection","MeshGenerationParams","MeshError",
                 "Cell","Face","FacePoint","MeshDiagnosticInfo","Breakline"})
            foreach (var asm in allAsm) { Type[] ts; try{ts=asm.GetTypes();}catch{continue;}
                foreach (var t in ts) if (t.Name==wanted && t.Namespace!=null
                    && (t.Namespace.StartsWith("Geospatial")||t.Namespace.StartsWith("Ras"))
                    && !dumpTargets.Contains(t)) dumpTargets.Add(t); }
        foreach (var t in dumpTargets) { Console.WriteLine(); DumpType(t); }

        // Also probe anything named *Mesh* across both assemblies (constructors that take points/faces).
        Console.WriteLine("\n[probe] ==== types matching *Mesh*/*Cell*/*Face* with a geometry-ish ctor ====");
        foreach (var asm in new[]{asmGeo, asmRas})
          foreach (var t in asm.GetTypes())
            if (t.Name.Contains("Mesh") || t.Name == "Cell" || t.Name == "Face")
              foreach (var c in t.GetConstructors(BindingFlags.Public|BindingFlags.NonPublic|BindingFlags.Instance))
              {
                var ps = c.GetParameters();
                if (ps.Any(p => p.ParameterType.Name.Contains("Point") || p.ParameterType.Name.Contains("[]") || p.ParameterType.Name.Contains("List") || p.ParameterType.Name.Contains("Cell") || p.ParameterType.Name.Contains("Face")))
                  Console.WriteLine("  " + t.FullName + " ctor(" + string.Join(", ", ps.Select(p=>p.ParameterType.Name+" "+p.Name)) + ")");
              }
        return 0;
    }
}
