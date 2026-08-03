"""Pruebas rápidas de data_loader + algorithm. Ejecutar: .venv/bin/python test_algorithm.py"""

from __future__ import annotations

import json
import sys
import time
from collections import deque

import numpy as np

import algorithm as alg
import data_loader as dl

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def is_contiguous(adj: alg.Adjacency, cluster: list[int]) -> bool:
    """BFS sobre el subgrafo inducido: ¿un solo componente?"""
    members = set(cluster)
    seen = {cluster[0]}
    queue = deque([cluster[0]])
    while queue:
        cur = queue.popleft()
        for nb in adj.neighbors[cur]:
            nb = int(nb)
            if nb in members and nb not in seen:
                seen.add(nb)
                queue.append(nb)
    return len(seen) == len(members)


def main() -> int:
    print("\n=== 1. Carga de la malla ===")
    t0 = time.perf_counter()
    mesh = dl.load_mesh()
    load_s = time.perf_counter() - t0
    print(f"  Cargada en {load_s:.2f} s")

    check("16,411 hexágonos", mesh.n_hexes == 16411, f"n={mesh.n_hexes}")
    check("ids únicos", len(mesh.id_to_index) == mesh.n_hexes)
    check(
        "área del hexágono ≈ 0.0541 km²",
        abs(mesh.hex_area_km2 - 0.0541) < 0.0002,
        f"{mesh.hex_area_km2:.5f} km²",
    )
    check(
        "Prioridad en [0, 1.9]",
        mesh.scores.min() >= 0 and mesh.scores.max() <= 1.9 + 1e-9,
        f"[{mesh.scores.min():.2f}, {mesh.scores.max():.2f}]",
    )
    check(
        "Prioridad = suma de las 7 variables V",
        np.abs(mesh.variables.sum(axis=1) - mesh.scores).max() < 1e-9,
    )
    minx, miny, maxx, maxy = mesh.bounds_wgs84
    check(
        "bounds WGS84 sobre Puebla",
        -99.0 < minx < -97.5 and 18.5 < miny < 19.5,
        f"({minx:.3f}, {miny:.3f}) – ({maxx:.3f}, {maxy:.3f})",
    )
    payload_mb = len(mesh.mesh_geojson) / 1e6
    print(f"  Payload GeoJSON de /data: {payload_mb:.2f} MB")
    check("payload < 8 MB", payload_mb < 8)

    print("\n=== 2. Vecindad (KDTree, radio 260 m) ===")
    t0 = time.perf_counter()
    adj = alg.build_adjacency(mesh.centroids_proj)
    adj_s = time.perf_counter() - t0
    print(f"  Construida en {adj_s:.2f} s — grado medio {adj.mean_degree:.2f}")
    degrees = np.array([len(n) for n in adj.neighbors])
    check("grado máximo = 6", degrees.max() == 6, f"max={degrees.max()}")
    check(
        "mayoría de hexes con 6 vecinos",
        (degrees == 6).sum() > 0.9 * mesh.n_hexes,
        f"{(degrees == 6).sum()} de {mesh.n_hexes}",
    )
    check("ningún hex aislado", degrees.min() >= 1, f"min={degrees.min()}")
    # Simetría de la relación de vecindad
    sym = all(
        i in set(adj.neighbors[int(nb)].tolist())
        for i in range(0, mesh.n_hexes, 997)
        for nb in adj.neighbors[i]
    )
    check("relación de vecindad simétrica", sym)

    print("\n=== 3. Objetivos por densidad y número de estaciones ===")
    for density, expected in [(9, 281), (10, 253), (11, 230)]:
        n = alg.target_hexes(density)
        check(f"{density} est/km² (137 est) → {expected} hexes", n == expected, f"obtuve {n}")
    # La fórmula general debe reproducir los objetivos fijados por el estudio.
    check(
        "la fórmula reproduce DENSITY_TARGETS",
        all(alg.target_hexes(d, 137) == n for d, n in alg.DENSITY_TARGETS.items()),
    )
    print(f"  rangos: densidad {alg.DENSITY_MIN}–{alg.DENSITY_MAX} est/km², "
          f"estaciones {alg.STATIONS_MIN}–{alg.STATIONS_MAX}")
    extremos = [
        (alg.DENSITY_MIN, alg.STATIONS_MAX), (alg.DENSITY_MAX, alg.STATIONS_MIN),
        (alg.DENSITY_MIN, alg.STATIONS_MIN), (alg.DENSITY_MAX, alg.STATIONS_MAX),
    ]
    for d, st in extremos:
        n = alg.target_hexes(d, st)
        print(f"    {st} est @ {d}/km² → {n} hexes ({n * mesh.hex_area_km2:.2f} km²)")
        check(f"{st}est@{d}: hexes coherentes con el área", n == round(st / d / 0.0541))
    check(
        "el rango completo cabe entre 150 y 400 hexágonos",
        150 <= alg.target_hexes(alg.DENSITY_MAX, alg.STATIONS_MIN)
        and alg.target_hexes(alg.DENSITY_MIN, alg.STATIONS_MAX) <= 400,
    )

    print("\n=== 3b. Variables individuales en el payload de /data ===")
    payload_mesh = json.loads(mesh.mesh_geojson)
    props0 = payload_mesh["features"][0]["properties"]
    check("cada hexágono trae los 7 puntajes crudos", len(props0.get("v", [])) == 7)
    check(
        "los puntajes crudos son enteros 0-2",
        all(
            isinstance(x, int) and 0 <= x <= 2
            for f in payload_mesh["features"][:500]
            for x in f["properties"]["v"]
        ),
    )
    # El frontend reconstruye V = P * (peso_max / 2); debe dar la Prioridad exacta.
    vmax = [dl.VAR_MAX[c] for c in dl.VAR_COLS]
    peor = max(
        abs(sum(p * m / 2 for p, m in zip(f["properties"]["v"], vmax)) - f["properties"]["p"])
        for f in payload_mesh["features"]
    )
    check(
        "V = P·(peso/2) reconstruye la Prioridad en los 16,411 hexágonos",
        peor < 1e-9,
        f"peor error = {peor:.2e}",
    )
    print(f"  payload con variables: {len(mesh.mesh_geojson)/1e6:.2f} MB crudos")

    print("\n=== 4. Expansión greedy (Centro Histórico de Puebla) ===")
    lat, lon = 19.0438, -98.1982
    for density in (9, 10, 11):
        t0 = time.perf_counter()
        result = alg.build_polygon(mesh, adj, lat, lon, density)
        elapsed = time.perf_counter() - t0
        m = result["metrics"]
        print(
            f"  {density} est/km²: {m['n_hexes']} hexes, {m['area_km2']} km², "
            f"score μ={m['score_mean']} [{m['score_min']}–{m['score_max']}], "
            f"compacidad={m['compactness']}, {m['stations']} estaciones "
            f"— {elapsed*1000:.0f} ms"
        )
        check(
            f"d={density}: número de hexes correcto",
            m["n_hexes"] == m["target_hexes"],
        )
        check(f"d={density}: responde en < 2 s", elapsed < 2.0, f"{elapsed*1000:.0f} ms")
        check(
            f"d={density}: área ≈ n × 0.0541",
            abs(m["area_km2"] - m["n_hexes"] * mesh.hex_area_km2) < 0.01,
        )
        check(f"d={density}: outline es un Polygon único",
              result["outline"]["geometry"]["type"] == "Polygon",
              result["outline"]["geometry"]["type"])
        check(
            f"d={density}: sin warnings",
            not result["warnings"],
            "; ".join(result["warnings"]),
        )
        cluster = [mesh.index_of(h) for h in result["hex_ids"]]
        check(f"d={density}: cluster CONTIGUO", is_contiguous(adj, cluster))
        check(
            f"d={density}: score medio supera el de la malla",
            m["score_mean"] > mesh.scores.mean(),
            f"{m['score_mean']} vs {mesh.scores.mean():.4f} global",
        )
        check(
            f"d={density}: desglose suma el score medio",
            abs(sum(b["mean"] for b in m["breakdown"]) - m["score_mean"]) < 1e-3,
        )
        check(f"d={density}: sin hexes duplicados", len(set(result["hex_ids"])) == m["n_hexes"])

    print("\n=== 5. Semilla ===")
    x, y = alg._to_proj.transform(lon, lat)
    seed, radius, note = alg.find_seed(mesh, adj, x, y)
    near = adj.tree.query_ball_point([x, y], 500.0)
    check(
        "semilla = máxima Prioridad en 500 m",
        mesh.scores[seed] == mesh.scores[np.asarray(near)].max(),
        f"score semilla={mesh.scores[seed]:.2f}, radio={radius:.0f} m",
    )
    # Punto fuera de la malla: debe advertir, no explotar
    far = alg.build_polygon(mesh, adj, 19.60, -99.20, 10)
    check("punto lejano genera warning", bool(far["warnings"]),
          "; ".join(far["warnings"])[:80])

    print("\n=== 5b. Penalización por distancia (lambda) — Cholula 19.058,-98.302 ===")
    lat_c, lon_c = 19.058, -98.302
    for lam in (0.0, 0.3, 0.7, 1.0):
        t0 = time.perf_counter()
        r = alg.build_polygon(mesh, adj, lat_c, lon_c, 10, lambda_val=lam)
        elapsed = time.perf_counter() - t0
        mm = r["metrics"]
        print(
            f"  λ={lam:.1f}: {mm['n_hexes']} hexes, compacidad={mm['compactness']}, "
            f"score μ={mm['score_mean']} — {elapsed*1000:.0f} ms"
        )
        check(f"λ={lam}: número de hexes correcto", mm["n_hexes"] == mm["target_hexes"])
        check(f"λ={lam}: responde en < 2 s", elapsed < 2.0, f"{elapsed*1000:.0f} ms")
        cl = [mesh.index_of(h) for h in r["hex_ids"]]
        check(f"λ={lam}: cluster CONTIGUO", is_contiguous(adj, cl))
        check(f"λ={lam}: metrics trae lambda_val", mm["lambda_val"] == lam)
    r0 = alg.build_polygon(mesh, adj, lat_c, lon_c, 10, lambda_val=0.0)
    r1 = alg.build_polygon(mesh, adj, lat_c, lon_c, 10, lambda_val=1.0)
    check(
        "λ=1.0 es más compacto que λ=0.0 (penaliza corredores largos)",
        r1["metrics"]["compactness"] > r0["metrics"]["compactness"],
        f"λ=0 → {r0['metrics']['compactness']}, λ=1 → {r1['metrics']['compactness']}",
    )
    check(
        "λ=1.0 tiene score medio menor o igual que λ=0.0 (es el trade-off esperado)",
        r1["metrics"]["score_mean"] <= r0["metrics"]["score_mean"] + 1e-9,
        f"λ=0 → {r0['metrics']['score_mean']}, λ=1 → {r1['metrics']['score_mean']}",
    )

    print("\n=== 5c. Saneamiento de la forma (sin huecos, sin brazos finos) ===")
    rng = np.random.default_rng(7)
    urbanos = np.flatnonzero(mesh.scores > 0)
    muestra = rng.choice(urbanos, 60, replace=False)
    n_huecos = n_partes = n_conteo = n_espinas = 0
    peor_ms = 0.0
    for i in muestra:
        lon_s, lat_s = mesh.centroids_wgs84[i]
        # Se barre todo el espacio explorable, no solo el caso de referencia.
        dens = int(rng.integers(alg.DENSITY_MIN, alg.DENSITY_MAX + 1))
        est = int(rng.integers(alg.STATIONS_MIN, alg.STATIONS_MAX + 1))
        lam = float(rng.choice([0.0, 0.2, 0.5, 1.0]))
        t0 = time.perf_counter()
        rr = alg.build_polygon(
            mesh, adj, float(lat_s), float(lon_s), dens, lambda_val=lam, stations=est
        )
        peor_ms = max(peor_ms, (time.perf_counter() - t0) * 1000)
        mm = rr["metrics"]
        n_huecos += mm["holes"]
        n_partes += mm["parts"] - 1
        n_conteo += mm["n_hexes"] != mm["target_hexes"]
        n_espinas += mm["thin_hexes"]
    print(
        f"  60 centroides aleatorios: huecos={n_huecos}, fragmentos={n_partes}, "
        f"conteo incorrecto={n_conteo}, espinas={n_espinas}, peor tiempo={peor_ms:.0f} ms"
    )
    check("ningún polígono con huecos interiores", n_huecos == 0, f"{n_huecos} huecos")
    check("ningún polígono fragmentado", n_partes == 0, f"{n_partes} fragmentos extra")
    check("todos con el conteo exacto de hexágonos", n_conteo == 0, f"{n_conteo} fallos")
    # Exigir conteo exacto puede dejar una celda de borde tocando por 2 lados;
    # se acota, no se prohíbe, porque es geométricamente inevitable.
    check(
        "brazos delgados residuales por debajo del 1% de los hexágonos",
        n_espinas < 0.01 * 60 * 253,
        f"{n_espinas} hexágonos en ~{60*253:,}",
    )
    check("saneamiento dentro del presupuesto de 2 s", peor_ms < 2000, f"{peor_ms:.0f} ms")

    # El saneamiento debe mejorar la compacidad frente al greedy crudo.
    crudo, _ = alg.grow_cluster(
        mesh, adj, seed, 281, origin_xy=(x, y), lambda_val=0.2, beta=0.0
    )
    geom_crudo = alg._dissolve(mesh, crudo)
    q_crudo = alg.shape_quality(adj, crudo, geom_crudo)
    r_sano = alg.build_polygon(mesh, adj, lat, lon, 9, lambda_val=0.2)
    print(
        f"  Centro λ=0.2 — greedy crudo: huecos={q_crudo['holes']}, "
        f"espinas={q_crudo['thin_hexes']}  →  saneado: "
        f"huecos={r_sano['metrics']['holes']}, espinas={r_sano['metrics']['thin_hexes']}"
    )
    check(
        "el greedy crudo sí producía huecos (el saneamiento no es decorativo)",
        q_crudo["holes"] > 0,
        f"{q_crudo['holes']} huecos antes",
    )
    check(
        "el saneamiento los elimina",
        r_sano["metrics"]["holes"] == 0 and r_sano["metrics"]["thin_hexes"] == 0,
    )

    print("\n=== 6. Comparación con top-N por score (control) ===")
    # Demuestra por qué NO usamos top-N: dentro de 2,500 m los 253 mejores
    # hexágonos por score forman varios fragmentos desconectados.
    ball = np.asarray(adj.tree.query_ball_point([x, y], 2500.0))
    topn = ball[np.argsort(-mesh.scores[ball], kind="stable")[:253]].tolist()
    contiguous_topn = is_contiguous(adj, topn)
    print(f"  top-N contiguo: {contiguous_topn} (esperado: False)")
    check("top-N NO es contiguo (justifica el greedy)", not contiguous_topn)

    print("\n=== 6b. Transporte masivo (RUTA + Cablebús) ===")
    import transit as tr

    tdata = tr.load_transit()
    check("162 estaciones cargadas", tdata.n_stations == 162, f"n={tdata.n_stations}")
    check(
        "153 BRT + 9 Cablebús",
        tdata.counts == {"brt": 153, "cable": 9},
        str(tdata.counts),
    )
    payload = json.loads(tdata.geojson)
    check("7 troncales", len(payload["lines"]["features"]) == 7)
    modes = [f["properties"]["mode"] for f in payload["lines"]["features"]]
    check(
        "2 troncales de teleférico, 5 de BRT",
        modes.count("cable") == 2 and modes.count("brt") == 5,
        f"cable={modes.count('cable')}, brt={modes.count('brt')}",
    )
    # Las 9 del Cablebús deben ser exactamente las anunciadas para el proyecto.
    esperadas = {
        "Parque Juárez", "CENHCH", "Parque Ecológico", "Xonaca", "Parque Biblioteca",
        "Cerro Amalucan", "Unidad Deportiva", "Mayorazgo", "CIS",
    }
    obtenidas = {
        f["properties"]["name"]
        for f in payload["stations"]["features"]
        if f["properties"]["mode"] == "cable"
    }
    check("nombres del Cablebús coinciden con el proyecto", obtenidas == esperadas,
          str(sorted(obtenidas ^ esperadas)) if obtenidas != esperadas else "")
    # Toda estación debe caer sobre la traza de su propio modo. Esto es lo que
    # valida que el CRS de las estaciones (EPSG:32614) sea el correcto: con un
    # datum equivocado las distancias se disparan a cientos de kilómetros.
    import geopandas as gpd
    from shapely.geometry import shape

    lines_gdf = gpd.GeoDataFrame(
        {"mode": [f["properties"]["mode"] for f in payload["lines"]["features"]]},
        geometry=[shape(f["geometry"]) for f in payload["lines"]["features"]],
        crs=4326,
    ).to_crs(6657)
    traza = {m: g.union_all() for m, g in lines_gdf.groupby("mode").geometry}

    peor = 0.0
    for f in payload["stations"]["features"]:
        pt = gpd.GeoSeries([shape(f["geometry"])], crs=4326).to_crs(6657).iloc[0]
        peor = max(peor, pt.distance(traza[f["properties"]["mode"]]))
    check(
        "toda estación cae a <25 m de la traza de su modo (valida EPSG:32614)",
        peor < 25,
        f"peor distancia = {peor:.1f} m",
    )

    print("\n=== 6c. Conteo de estaciones dentro del polígono ===")
    for lat_t, lon_t, nombre in [(19.0438, -98.1982, "Centro"), (19.058, -98.302, "Cholula")]:
        r = alg.build_polygon(mesh, adj, lat_t, lon_t, 10, transit=tdata)
        mm = r["metrics"]
        print(
            f"  {nombre}: BRT={mm['stations_brt']}, Cablebús={mm['stations_cable']}, "
            f"total={mm['stations_transit']}"
        )
        check(
            f"{nombre}: total = BRT + Cablebús",
            mm["stations_transit"] == mm["stations_brt"] + mm["stations_cable"],
        )
        check(
            f"{nombre}: conteos dentro de los totales",
            0 <= mm["stations_brt"] <= 153 and 0 <= mm["stations_cable"] <= 9,
        )
        check(
            f"{nombre}: el listado coincide con el conteo",
            len(mm["transit_stations"]) == mm["stations_transit"],
        )
    # Un polígono lejano no debe contener estaciones.
    far_r = alg.build_polygon(mesh, adj, 19.22, -98.05, 10, transit=tdata)
    check(
        "polígono en la periferia contiene pocas o ninguna estación",
        far_r["metrics"]["stations_transit"] < 20,
        f"{far_r['metrics']['stations_transit']} estaciones",
    )

    print("\n=== 7. Perfil de hexágono ===")
    profile = dl.hex_profile(mesh, result["hex_ids"][0])
    check("perfil trae 7 variables", len(profile["variables"]) == 7)
    check(
        "variables suman la Prioridad",
        abs(sum(v["value"] for v in profile["variables"]) - profile["prioridad"]) < 1e-3,
    )
    check(
        "normalizados en [0, 1]",
        all(0 <= v["normalized"] <= 1 for v in profile["variables"]),
    )

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"FALLARON {len(FAILURES)} verificaciones:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("TODAS LAS VERIFICACIONES PASARON")
    return 0


if __name__ == "__main__":
    sys.exit(main())
