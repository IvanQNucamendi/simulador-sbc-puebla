"""Carga y pre-procesamiento de la malla hexagonal al arranque del servidor.

Todo el trabajo caro (lectura del GeoPackage, reproyección, simplificación y
serialización a GeoJSON) se hace una sola vez y queda cacheado en memoria
dentro de un objeto `MeshData`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import geopandas as gpd
import numpy as np
from shapely.geometry import MultiPolygon

# --- Configuración del dataset -------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

# El archivo entregado se llama "Malla 2.gpkg" (con espacio); aceptamos ambas
# variantes para no romper si se renombra.
GPKG_CANDIDATES = ("Malla 2.gpkg", "Malla_2.gpkg")
LAYER_NAME = "malla_prueba_2"

CRS_SOURCE = "EPSG:6657"  # proyectado en metros
CRS_WGS84 = "EPSG:4326"

SCORE_COL = "Prioridad"
VAR_COLS = [
    "Densidad V",
    "Empleo V",
    "RUTA V",
    "AVerde V",
    "Ciclista V",
    "TuristicoV",
    "Escuelas V",
]
# Puntajes crudos (0-5) que dan origen a cada V; útiles para el perfil del hex.
RAW_COLS = [
    "Densidad P",
    "Empleo P",
    "RUTA P",
    "AVerde P",
    "Ciclista P",
    "TuristicoP",
    "Escuelas P",
]
# Peso máximo de cada variable (su aporte tope al score de Prioridad).
VAR_MAX = {
    "Densidad V": 0.2,
    "Empleo V": 0.4,
    "RUTA V": 0.5,
    "AVerde V": 0.2,
    "Ciclista V": 0.3,
    "TuristicoV": 0.2,
    "Escuelas V": 0.2,
}
VAR_LABELS = {
    "Densidad V": "Densidad poblacional",
    "Empleo V": "Empleo",
    "RUTA V": "Transporte masivo (RUTA)",
    "AVerde V": "Áreas verdes",
    "Ciclista V": "Infraestructura ciclista",
    "TuristicoV": "Atractivo turístico",
    "Escuelas V": "Escuelas",
}

SIMPLIFY_TOLERANCE_M = 10.0
COORD_PRECISION = 5  # ~1 m en latitud; recorta el payload a la mitad


def _find_gpkg() -> Path:
    for name in GPKG_CANDIDATES:
        path = BASE_DIR / name
        if path.exists():
            return path
    found = sorted(BASE_DIR.glob("*.gpkg"))
    if found:
        return found[0]
    raise FileNotFoundError(
        f"No se encontró el GeoPackage. Busqué {GPKG_CANDIDATES} en {BASE_DIR}"
    )


def _exterior_ring(geom) -> np.ndarray:
    """Anillo exterior del polígono (el mayor, si es MultiPolygon)."""
    if isinstance(geom, MultiPolygon):
        geom = max(geom.geoms, key=lambda g: g.area)
    return np.asarray(geom.exterior.coords)


@dataclass
class MeshData:
    """Malla hexagonal pre-procesada y lista para servir."""

    # Índice posicional 0..N-1 es la clave interna; `ids` mapea a los id del gpkg.
    ids: np.ndarray                      # int64  (N,)
    scores: np.ndarray                   # float64 (N,)
    variables: np.ndarray                # float64 (N, 7) en orden VAR_COLS
    raw_scores: np.ndarray               # int32   (N, 7) en orden RAW_COLS
    centroids_proj: np.ndarray           # float64 (N, 2) en EPSG:6657
    centroids_wgs84: np.ndarray          # float64 (N, 2) como (lon, lat)
    geoms_proj: gpd.GeoSeries            # geometrías originales (para dissolve)
    id_to_index: dict[int, int]
    hex_area_km2: float
    bounds_wgs84: tuple[float, float, float, float]
    mesh_geojson: bytes                  # payload cacheado de GET /data
    score_histogram: dict = field(default_factory=dict)

    @property
    def n_hexes(self) -> int:
        return len(self.ids)

    def index_of(self, hex_id: int) -> int:
        try:
            return self.id_to_index[int(hex_id)]
        except KeyError as exc:
            raise KeyError(f"hexágono {hex_id} inexistente") from exc


def _build_mesh_geojson(
    ids: np.ndarray,
    scores: np.ndarray,
    centroids_wgs84: np.ndarray,
    rings_wgs84: list[np.ndarray],
    raw_scores: np.ndarray,
) -> bytes:
    """GeoJSON compacto de la malla completa.

    Cada hexágono lleva id, Prioridad, centroide y el campo `v`: los 7 puntajes
    crudos (0, 1 o 2) en el orden de VAR_COLS. Se mandan esos enteros y no los
    valores ponderados porque `V = P * (peso_máximo / 2)` es exacto para las
    siete variables, así que el frontend los reconstruye sin pérdida y el
    payload crece ~224 KB en vez de ~700 KB.
    """
    features = []
    for i in range(len(ids)):
        ring = np.round(rings_wgs84[i], COORD_PRECISION)
        features.append(
            {
                "type": "Feature",
                "id": int(ids[i]),
                "properties": {
                    "id": int(ids[i]),
                    "p": round(float(scores[i]), 3),
                    "cx": round(float(centroids_wgs84[i, 0]), COORD_PRECISION),
                    "cy": round(float(centroids_wgs84[i, 1]), COORD_PRECISION),
                    "v": raw_scores[i].tolist(),
                },
                "geometry": {"type": "Polygon", "coordinates": [ring.tolist()]},
            }
        )
    payload = {"type": "FeatureCollection", "features": features}
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _score_histogram(scores: np.ndarray, bins: int = 40) -> dict:
    counts, edges = np.histogram(scores, bins=bins, range=(0.0, float(scores.max())))
    return {
        "counts": counts.astype(int).tolist(),
        "edges": [round(float(e), 4) for e in edges],
    }


def load_mesh(gpkg_path: Path | str | None = None) -> MeshData:
    """Lee el GeoPackage y devuelve la malla pre-procesada."""
    path = Path(gpkg_path) if gpkg_path else _find_gpkg()
    gdf = gpd.read_file(path, layer=LAYER_NAME)

    if gdf.crs is None:
        gdf = gdf.set_crs(CRS_SOURCE)
    elif gdf.crs.to_string() != CRS_SOURCE:
        gdf = gdf.to_crs(CRS_SOURCE)

    missing = [c for c in [SCORE_COL, *VAR_COLS] if c not in gdf.columns]
    if missing:
        raise ValueError(f"Faltan columnas en {path.name}: {missing}")

    gdf = gdf.reset_index(drop=True)

    ids = gdf["id"].to_numpy(dtype=np.int64)
    if len(set(ids.tolist())) != len(ids):
        raise ValueError("La columna 'id' tiene duplicados; no puede usarse como clave")

    scores = gdf[SCORE_COL].to_numpy(dtype=np.float64)
    variables = gdf[VAR_COLS].to_numpy(dtype=np.float64)
    raw_scores = (
        gdf[RAW_COLS].to_numpy(dtype=np.int32)
        if all(c in gdf.columns for c in RAW_COLS)
        else np.zeros((len(gdf), len(VAR_COLS)), dtype=np.int32)
    )

    geoms_proj = gdf.geometry
    centroids = geoms_proj.centroid
    centroids_proj = np.c_[centroids.x.to_numpy(), centroids.y.to_numpy()]

    centroids_ll = centroids.to_crs(CRS_WGS84)
    centroids_wgs84 = np.c_[centroids_ll.x.to_numpy(), centroids_ll.y.to_numpy()]

    # Simplificamos en metros (donde la tolerancia tiene sentido) y luego
    # reproyectamos para el mapa.
    simplified = geoms_proj.simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=False)
    rings_wgs84 = [_exterior_ring(g) for g in simplified.to_crs(CRS_WGS84)]

    hex_area_km2 = float(geoms_proj.area.median()) / 1e6

    minx, miny, maxx, maxy = gpd.GeoSeries(
        geoms_proj, crs=CRS_SOURCE
    ).to_crs(CRS_WGS84).total_bounds

    mesh = MeshData(
        ids=ids,
        scores=scores,
        variables=variables,
        raw_scores=raw_scores,
        centroids_proj=centroids_proj,
        centroids_wgs84=centroids_wgs84,
        geoms_proj=geoms_proj,
        id_to_index={int(v): i for i, v in enumerate(ids)},
        hex_area_km2=hex_area_km2,
        bounds_wgs84=(float(minx), float(miny), float(maxx), float(maxy)),
        mesh_geojson=_build_mesh_geojson(
            ids, scores, centroids_wgs84, rings_wgs84, raw_scores
        ),
        score_histogram=_score_histogram(scores),
    )
    return mesh


def hex_profile(mesh: MeshData, hex_id: int) -> dict:
    """Perfil completo de un hexágono para el panel lateral."""
    i = mesh.index_of(hex_id)
    variables = []
    for j, col in enumerate(VAR_COLS):
        vmax = VAR_MAX[col]
        value = float(mesh.variables[i, j])
        variables.append(
            {
                "key": col,
                "label": VAR_LABELS[col],
                "value": round(value, 4),
                "max": vmax,
                "normalized": round(value / vmax, 4) if vmax else 0.0,
                "raw": int(mesh.raw_scores[i, j]),
            }
        )
    return {
        "id": int(mesh.ids[i]),
        "prioridad": round(float(mesh.scores[i]), 4),
        "area_km2": round(mesh.hex_area_km2, 5),
        "lon": round(float(mesh.centroids_wgs84[i, 0]), 6),
        "lat": round(float(mesh.centroids_wgs84[i, 1]), 6),
        "variables": variables,
    }
