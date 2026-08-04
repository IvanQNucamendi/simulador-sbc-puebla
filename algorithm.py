"""Construcción del polígono piloto: vecindad pre-computada + expansión greedy.

La contigüidad está garantizada por construcción: un hexágono solo puede entrar
al cluster si es vecino de alguno que ya está dentro. Nunca se seleccionan
"top-N por score", que produciría manchas dispersas.
"""

from __future__ import annotations

import heapq
import math
from collections import deque
from dataclasses import dataclass

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree
from shapely import union_all
from shapely.geometry import MultiPolygon, mapping
from shapely.ops import transform as shapely_transform

from data_loader import CRS_SOURCE, CRS_WGS84, VAR_COLS, VAR_LABELS, VAR_MAX, MeshData

# --- Parámetros del algoritmo --------------------------------------------------

HEX_SPACING_M = 250.0
NEIGHBOR_RADIUS_M = 260.0        # 250 m + ~4% de tolerancia
SEED_SEARCH_RADIUS_M = 500.0     # radio primario para elegir el hex semilla
SEED_FALLBACK_RADII_M = (500.0, 1_000.0, 2_500.0, 5_000.0)

# Radio de búsqueda de referencia para normalizar la penalización por
# distancia (spec original: "mínimo 2,500 m para tener hexágonos suficientes").
SEARCH_RADIUS_M = 2_500.0
DEFAULT_LAMBDA = 0.75

# --- Saneamiento de la forma ---------------------------------------------------
# Bono de cohesión: cada candidato suma β·(lados_en_contacto/6) a su score
# efectivo, así las concavidades se rellenan antes de que crezcan tentáculos.
COHESION_BETA = 0.35
# Un hexágono en un brazo de ancho 1 toca el cluster por 2 lados; uno en un
# borde normal, por 3 o más. Podar por debajo de 3 fija el grosor mínimo del
# polígono en 2 hexágonos (~500 m), que es el criterio operativo del piloto.
MIN_SIDE_CONTACTS = 3
# Un "hueco" mayor que el propio polígono no es un hueco: es el exterior.
MAX_REFINE_PASSES = 12

STATIONS_TOTAL = 137           # tamaño del sistema completo (caso de referencia)
HEX_AREA_KM2_NOMINAL = 0.0541

# Rangos explorables desde el panel.
DENSITY_MIN, DENSITY_MAX = 7, 12        # estaciones por km²
STATIONS_MIN, STATIONS_MAX = 100, 150   # estaciones del piloto

# Hexágonos objetivo del sistema completo por densidad, según el estudio.
# Se conservan como referencia: la fórmula general los reproduce exactamente.
DENSITY_TARGETS: dict[int, int] = {9: 281, 10: 253, 11: 230}

_to_proj = Transformer.from_crs(CRS_WGS84, CRS_SOURCE, always_xy=True)
_to_wgs84 = Transformer.from_crs(CRS_SOURCE, CRS_WGS84, always_xy=True)


def target_hexes(density: int, stations: int = STATIONS_TOTAL) -> int:
    """Hexágonos necesarios para `stations` estaciones a `density` est/km².

    Con las 137 del sistema completo reproduce exactamente los objetivos del
    estudio: 281 (9/km²), 253 (10/km²) y 230 (11/km²).
    """
    if density <= 0:
        raise ValueError("La densidad debe ser positiva")
    if stations <= 0:
        raise ValueError("El número de estaciones debe ser positivo")
    return int(round(stations / density / HEX_AREA_KM2_NOMINAL))


# --- Vecindad ------------------------------------------------------------------


@dataclass
class Adjacency:
    """Vecinos pre-computados por hexágono (índices posicionales)."""

    tree: cKDTree
    neighbors: list[np.ndarray]

    @property
    def mean_degree(self) -> float:
        return float(np.mean([len(n) for n in self.neighbors]))


def build_adjacency(
    centroids_proj: np.ndarray, radius_m: float = NEIGHBOR_RADIUS_M
) -> Adjacency:
    """KDTree + diccionario de vecinos a distancia <= radius_m entre centroides."""
    tree = cKDTree(centroids_proj)
    pairs = tree.query_pairs(radius_m, output_type="ndarray")

    n = len(centroids_proj)
    # Duplicamos cada par en ambos sentidos y agrupamos con un ordenamiento.
    src = np.concatenate([pairs[:, 0], pairs[:, 1]])
    dst = np.concatenate([pairs[:, 1], pairs[:, 0]])
    order = np.argsort(src, kind="stable")
    src, dst = src[order], dst[order]
    starts = np.searchsorted(src, np.arange(n), side="left")
    ends = np.searchsorted(src, np.arange(n), side="right")
    neighbors = [dst[starts[i] : ends[i]] for i in range(n)]
    return Adjacency(tree=tree, neighbors=neighbors)


# --- Semilla y expansión -------------------------------------------------------


def find_seed(
    mesh: MeshData, adj: Adjacency, x: float, y: float
) -> tuple[int, float, str]:
    """Hexágono de mayor Prioridad cerca del punto (x, y) en EPSG:6657.

    Devuelve (índice, radio usado, nota). Si dentro de 500 m no hay nada,
    amplía el radio en pasos y lo reporta.
    """
    for radius in SEED_FALLBACK_RADII_M:
        candidates = adj.tree.query_ball_point([x, y], radius)
        if not candidates:
            continue
        cand = np.asarray(candidates)
        best_score = mesh.scores[cand].max()
        # Entre empates de score, el más cercano al punto del usuario.
        tied = cand[mesh.scores[cand] == best_score]
        d2 = ((mesh.centroids_proj[tied] - np.array([x, y])) ** 2).sum(axis=1)
        seed = int(tied[int(np.argmin(d2))])
        note = (
            ""
            if radius == SEED_SEARCH_RADIUS_M
            else f"Sin hexágonos en {SEED_SEARCH_RADIUS_M:.0f} m; "
            f"semilla tomada a {radius:.0f} m."
        )
        return seed, radius, note

    # Último recurso: el hexágono más cercano de toda la malla.
    _, seed = adj.tree.query([x, y], k=1)
    return int(seed), math.inf, "El punto cae muy lejos de la malla."


def grow_cluster(
    mesh: MeshData,
    adj: Adjacency,
    seed: int,
    n_target: int,
    origin_xy: tuple[float, float] | None = None,
    lambda_val: float = DEFAULT_LAMBDA,
    beta: float = COHESION_BETA,
) -> tuple[list[int], bool]:
    """Expansión greedy contigua desde `seed` hasta `n_target` hexágonos.

    En cada iteración se toma del heap el candidato frontera con mayor score
    efectivo:

        score_efectivo = Prioridad
                       - lambda_val * (dist_al_origen / SEARCH_RADIUS_M)
                       + beta * (lados_en_contacto / 6)

    `origin_xy` es el centroide que arrastra el usuario (en EPSG:6657); por
    defecto, si no se da, es el propio centroide de la semilla. `lambda_val`
    penaliza alejarse del centroide; `beta` premia encajar en una concavidad,
    de modo que el frente rellena entrantes antes de estirar tentáculos.

    El bono de cohesión cambia conforme crece el cluster, así que la clave de
    un candidato queda obsoleta en cuanto gana un vecino. En vez de reconstruir
    el heap entero (O(N²)), cada entrada guarda la cohesión con la que se
    calculó y al extraerla se descarta si ya cambió: como la cohesión solo
    puede subir de 0 a 6, cada hexágono se reinserta 6 veces como máximo.

    Devuelve (índices del cluster, completo?).
    """
    scores = mesh.scores
    centroids = mesh.centroids_proj
    ox, oy = origin_xy if origin_xy is not None else tuple(centroids[seed])

    def dist_to_origin(i: int) -> float:
        return math.hypot(centroids[i, 0] - ox, centroids[i, 1] - oy)

    cohesion = np.zeros(mesh.n_hexes, dtype=np.int8)  # lados en contacto
    in_cluster = np.zeros(mesh.n_hexes, dtype=bool)

    # Max-heap: (-score_efectivo, distancia al origen, índice, cohesión usada)
    heap: list[tuple[float, float, int, int]] = []

    def push(i: int) -> None:
        d = dist_to_origin(i)
        c = int(cohesion[i])
        eff = scores[i] - lambda_val * (d / SEARCH_RADIUS_M) + beta * (c / 6.0)
        heapq.heappush(heap, (-eff, d, i, c))

    def absorb(i: int) -> None:
        in_cluster[i] = True
        for nb in adj.neighbors[i]:
            nb = int(nb)
            cohesion[nb] += 1
            if not in_cluster[nb]:
                push(nb)  # su cohesión subió: necesita una clave nueva

    cluster: list[int] = [seed]
    absorb(seed)

    while len(cluster) < n_target and heap:
        _, _, best, c_used = heapq.heappop(heap)
        if in_cluster[best] or c_used != cohesion[best]:
            continue  # ya está dentro, o la clave quedó obsoleta
        cluster.append(best)
        absorb(best)

    return cluster, len(cluster) >= n_target


# --- Saneamiento topológico de la forma ---------------------------------------


def _in_cluster_degree(adj: Adjacency, cs: set[int]) -> dict[int, int]:
    """Lados de cada hexágono del cluster que tocan a otro del cluster."""
    return {i: sum(1 for nb in adj.neighbors[i] if int(nb) in cs) for i in cs}


def _largest_component(adj: Adjacency, cs: set[int]) -> set[int]:
    """Mayor componente conexa del cluster (la poda puede partirlo)."""
    seen: set[int] = set()
    best: set[int] = set()
    for start in cs:
        if start in seen:
            continue
        comp = {start}
        seen.add(start)
        queue = deque([start])
        while queue:
            for nb in adj.neighbors[queue.popleft()]:
                nb = int(nb)
                if nb in cs and nb not in seen:
                    seen.add(nb)
                    comp.add(nb)
                    queue.append(nb)
        if len(comp) > len(best):
            best = comp
    return best


def fill_holes(adj: Adjacency, cs: set[int], cap: int) -> set[int]:
    """Absorbe todo bolsillo de hexágonos encerrado por el cluster.

    Explora el complemento desde la frontera del cluster. Un bolsillo cuya
    exploración se cierra sin superar `cap` hexágonos está encerrado y se
    rellena; si la supera, es el exterior y se marca como tal para no volver a
    recorrerlo. Así no hace falta barrer los 16k hexágonos de la malla.
    """
    outside: set[int] = set()
    filled: set[int] = set()

    frontier = {
        int(nb) for i in cs for nb in adj.neighbors[i] if int(nb) not in cs
    }
    for start in frontier:
        if start in outside or start in filled:
            continue
        pocket = {start}
        queue = deque([start])
        escaped = False
        while queue:
            cur = queue.popleft()
            for nb in adj.neighbors[cur]:
                nb = int(nb)
                if nb in cs or nb in pocket:
                    continue
                if nb in outside:
                    escaped = True
                    break
                pocket.add(nb)
                queue.append(nb)
            if escaped or len(pocket) > cap:
                escaped = True
                break
        # Un hexágono de borde de malla tiene <6 vecinos: nunca está encerrado.
        if not escaped:
            escaped = any(len(adj.neighbors[i]) < 6 for i in pocket)
        (outside if escaped else filled).update(pocket)

    return cs | filled


def prune_thin(
    adj: Adjacency, cs: set[int], min_contacts: int = MIN_SIDE_CONTACTS
) -> set[int]:
    """Poda iterativa de hexágonos unidos al cluster por muy pocos lados.

    Con `min_contacts=3` desaparecen los brazos de un hexágono de ancho (sus
    celdas tocan solo por 2 lados) y el polígono queda con un grosor mínimo de
    2 hexágonos, ~500 m.
    """
    cs = set(cs)
    for _ in range(MAX_REFINE_PASSES):
        degree = _in_cluster_degree(adj, cs)
        drop = {i for i, d in degree.items() if d < min_contacts}
        if not drop or len(drop) >= len(cs):
            break
        cs -= drop
        cs = _largest_component(adj, cs)
    return cs


def refine_shape(
    mesh: MeshData,
    adj: Adjacency,
    cluster: list[int],
    n_target: int,
    origin_xy: tuple[float, float],
    lambda_val: float,
    min_contacts: int = MIN_SIDE_CONTACTS,
) -> list[int]:
    """Deja el cluster sin huecos, sin espinas y con exactamente n_target hexes.

    Rellenar añade hexágonos y podar quita, así que al final se reajusta el
    conteo: para crecer se elige el candidato mejor encajado (más lados en
    contacto) y para encoger, el peor encajado, de modo que el reajuste no
    reintroduce las patologías que se acaban de eliminar.
    """
    scores = mesh.scores
    centroids = mesh.centroids_proj
    ox, oy = origin_xy

    def effective(i: int) -> float:
        d = math.hypot(centroids[i, 0] - ox, centroids[i, 1] - oy)
        return scores[i] - lambda_val * (d / SEARCH_RADIUS_M)

    def resize_to_target(cs: set[int]) -> set[int]:
        """Lleva el cluster a n_target sin deshacer el saneamiento."""
        for _ in range(4 * n_target):
            if len(cs) == n_target:
                break
            if len(cs) < n_target:
                cand = {int(nb) for i in cs for nb in adj.neighbors[i]} - cs
                if not cand:
                    break
                contacts = {
                    i: sum(1 for nb in adj.neighbors[i] if int(nb) in cs)
                    for i in cand
                }
                cs.add(max(cand, key=lambda i: (contacts[i], effective(i))))
            else:
                degree = _in_cluster_degree(adj, cs)
                cs.discard(min(cs, key=lambda i: (degree[i], effective(i))))
            cs = fill_holes(adj, cs, cap=n_target)
        return cs

    def defects(cs: set[int]) -> tuple[int, int, int]:
        """(desvío del conteo, hexágonos encerrados, espinas).

        El orden es la jerarquía de gravedad. El conteo va primero: es un
        requisito duro, porque de él sale el número de estaciones del piloto.
        Después los huecos, que es lo que hace inoperable el área. Una espina
        suelta en el borde es el defecto más leve de los tres.
        """
        degree = _in_cluster_degree(adj, cs)
        thin = sum(1 for d in degree.values() if d < min_contacts)
        enclosed = len(fill_holes(adj, cs, cap=n_target) - cs)
        return abs(len(cs) - n_target), enclosed, thin

    def repair_by_swap(cs: set[int]) -> set[int]:
        """Elimina espinas sueltas sin alterar el conteo, intercambiando.

        Exigir un conteo exacto deja a veces una última celda pegada a un borde
        ya liso, que toca por solo 2 lados. En vez de quitarla (el reajuste la
        repondría), se rellena la muesca a su lado y se compensa soltando el
        hexágono de menor valor de un tramo grueso del borde.
        """
        for _ in range(MAX_REFINE_PASSES):
            degree = _in_cluster_degree(adj, cs)
            spines = [i for i, d in degree.items() if d < min_contacts]
            if not spines:
                break
            spine = min(spines, key=lambda i: degree[i])

            # Vecinos libres de la espina, mejor encajados primero.
            fillers = sorted(
                (int(nb) for nb in adj.neighbors[spine] if int(nb) not in cs),
                key=lambda i: (
                    sum(1 for nb in adj.neighbors[i] if int(nb) in cs),
                    effective(i),
                ),
                reverse=True,
            )
            # Se suelta desde un tramo grueso del borde, de menor valor primero.
            donors = sorted(
                (i for i, d in degree.items() if d >= 5 and i != spine),
                key=effective,
            )
            if not fillers or not donors:
                break

            # Rellenar puede sellar una muesca y crear un hueco, así que el
            # intercambio se acepta solo si el balance global mejora; se prueban
            # varias combinaciones antes de rendirse.
            current = defects(cs)
            best_trial, best_score = None, current
            for filler in fillers[:3]:
                for donor in donors[:3]:
                    trial = (cs | {filler}) - {donor}  # neutro en conteo
                    score = defects(trial)
                    if score < best_score:
                        best_trial, best_score = trial, score
            if best_trial is None:
                break
            cs = best_trial
        return cs

    cs = set(cluster)
    best = cs
    best_defects = (len(cs), n_target)

    # Rellenar añade y podar quita, así que hay que reajustar el conteo — y el
    # reajuste puede volver a dejar una celda mal encajada al final. Se itera el
    # ciclo completo y se conserva el estado más limpio alcanzado, en vez de
    # confiar en que una sola pasada converja.
    for _ in range(MAX_REFINE_PASSES):
        cs = fill_holes(adj, cs, cap=n_target)
        cs = prune_thin(adj, cs, min_contacts)
        if not cs:
            break
        cs = resize_to_target(cs)
        cs = fill_holes(adj, cs, cap=n_target)
        cs = repair_by_swap(cs)

        d = defects(cs)
        if d < best_defects:
            best, best_defects = set(cs), d
        if d == (0, 0):
            break

    return sorted(best) if best else sorted(cluster)


def shape_quality(adj: Adjacency, cluster: list[int], geom) -> dict:
    """Diagnóstico de la forma entregada, para poder verificarlo en la UI."""
    cs = set(cluster)
    degree = _in_cluster_degree(adj, cs)
    parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
    return {
        "holes": int(sum(len(p.interiors) for p in parts)),
        "parts": int(len(parts)),
        "thin_hexes": int(sum(1 for d in degree.values() if d < MIN_SIDE_CONTACTS)),
        "min_side_contacts": int(min(degree.values())) if degree else 0,
    }


# --- Métricas y geometría de salida -------------------------------------------


def _dissolve(mesh: MeshData, cluster: list[int]):
    """Unión geométrica del cluster en EPSG:6657."""
    geom = union_all(mesh.geoms_proj.iloc[cluster].to_numpy())
    if isinstance(geom, MultiPolygon):
        # Sutura micro-slivers si el grid tuviera vértices no coincidentes.
        geom = geom.buffer(0.05).buffer(-0.05)
    return geom


def _to_wgs84_geom(geom):
    return shapely_transform(lambda xs, ys: _to_wgs84.transform(xs, ys), geom)


def cluster_metrics(mesh: MeshData, cluster: list[int], geom, density: int) -> dict:
    idx = np.asarray(cluster)
    scores = mesh.scores[idx]
    variables = mesh.variables[idx]

    area_km2 = geom.area / 1e6
    perimeter_m = geom.length
    # Polsby-Popper: 1.0 = círculo perfecto. Mide compacidad del polígono.
    compactness = (
        4 * math.pi * geom.area / (perimeter_m**2) if perimeter_m > 0 else 0.0
    )

    breakdown = []
    for j, col in enumerate(VAR_COLS):
        mean_v = float(variables[:, j].mean())
        breakdown.append(
            {
                "key": col,
                "label": VAR_LABELS[col],
                "mean": round(mean_v, 4),
                "max": VAR_MAX[col],
                "share_of_max": round(mean_v / VAR_MAX[col], 4) if VAR_MAX[col] else 0.0,
                "share_of_score": (
                    round(mean_v / float(scores.mean()), 4) if scores.mean() > 0 else 0.0
                ),
            }
        )

    return {
        "n_hexes": int(len(cluster)),
        "area_km2": round(area_km2, 4),
        "score_mean": round(float(scores.mean()), 4),
        "score_min": round(float(scores.min()), 4),
        "score_max": round(float(scores.max()), 4),
        "score_median": round(float(np.median(scores)), 4),
        "score_sum": round(float(scores.sum()), 3),
        "density": density,
        # Estaciones que caben en el área realmente construida. Puede diferir
        # en 1-2 del objetivo pedido porque el conteo de hexágonos se redondea.
        "stations": int(round(area_km2 * density)),
        "perimeter_km": round(perimeter_m / 1000, 3),
        "compactness": round(compactness, 4),
        "breakdown": breakdown,
    }


def build_polygon(
    mesh: MeshData,
    adj: Adjacency,
    lat: float,
    lon: float,
    density: int,
    lambda_val: float = DEFAULT_LAMBDA,
    transit=None,
    stations: int = STATIONS_TOTAL,
) -> dict:
    """Pipeline completo: punto del usuario -> polígono piloto + métricas."""
    n_target = target_hexes(density, stations)
    x, y = _to_proj.transform(lon, lat)

    seed, seed_radius, note = find_seed(mesh, adj, x, y)
    cluster, complete = grow_cluster(
        mesh, adj, seed, n_target, origin_xy=(x, y), lambda_val=lambda_val
    )

    warnings = [w for w in [note] if w]
    if not complete:
        warnings.append(
            f"El componente contiguo alcanzado solo tiene {len(cluster)} "
            f"hexágonos de los {n_target} requeridos."
        )

    # Un área de servicio con huecos interiores o brazos de un hexágono de
    # ancho no es operable en calle: se sanea la forma antes de entregarla.
    if complete:
        cluster = refine_shape(
            mesh, adj, cluster, n_target, (x, y), lambda_val
        )

    geom_proj = _dissolve(mesh, cluster)
    outline = _to_wgs84_geom(geom_proj)

    # Hexágonos miembros (simplificados) para permitir click individual.
    members_ll = (
        mesh.geoms_proj.iloc[cluster]
        .simplify(10.0, preserve_topology=False)
        .to_crs(CRS_WGS84)
    )
    hex_features = [
        {
            "type": "Feature",
            "id": int(mesh.ids[i]),
            "properties": {
                "id": int(mesh.ids[i]),
                "p": round(float(mesh.scores[i]), 3),
                "rank": rank,
            },
            "geometry": mapping(g),
        }
        for rank, (i, g) in enumerate(zip(cluster, members_ll))
    ]

    metrics = cluster_metrics(mesh, cluster, geom_proj, density)
    metrics["target_hexes"] = n_target
    metrics["seed_id"] = int(mesh.ids[seed])
    metrics["seed_radius_m"] = None if math.isinf(seed_radius) else seed_radius
    metrics["lambda_val"] = lambda_val
    metrics["stations_target"] = stations
    metrics.update(shape_quality(adj, cluster, geom_proj))

    # Cobertura de transporte masivo dentro del polígono: cuántas estaciones
    # de RUTA y del Cablebús quedarían servidas por el piloto.
    if transit is not None:
        import transit as tr

        cov = tr.stations_within(transit, geom_proj)
        metrics["stations_brt"] = cov["brt"]
        metrics["stations_cable"] = cov["cable"]
        metrics["stations_transit"] = cov["total"]
        metrics["transit_stations"] = cov["stations"]

    centroid_ll = _to_wgs84_geom(geom_proj.centroid)

    return {
        "metrics": metrics,
        "warnings": warnings,
        "centroid": {"lat": round(centroid_ll.y, 6), "lon": round(centroid_ll.x, 6)},
        "request": {"lat": lat, "lon": lon, "density": density, "lambda_val": lambda_val},
        "outline": {
            "type": "Feature",
            "properties": {"kind": "outline"},
            "geometry": mapping(outline),
        },
        "hexes": {"type": "FeatureCollection", "features": hex_features},
        "hex_ids": [int(mesh.ids[i]) for i in cluster],
    }
