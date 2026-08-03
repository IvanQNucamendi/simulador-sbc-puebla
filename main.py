"""API del explorador de polígonos piloto del SBC de Puebla.

Arranque:  .venv/bin/uvicorn main:app --reload --port 8000
Luego abrir http://127.0.0.1:8000
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import algorithm as alg
import data_loader as dl
import transit as tr

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("sbc")

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Estado cacheado al arranque (la malla nunca se relee por request).
STATE: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    t0 = time.perf_counter()
    mesh = dl.load_mesh()
    log.info(
        "Malla cargada: %d hexágonos, %.2f MB de GeoJSON (%.2f s)",
        mesh.n_hexes,
        len(mesh.mesh_geojson) / 1e6,
        time.perf_counter() - t0,
    )
    t1 = time.perf_counter()
    adj = alg.build_adjacency(mesh.centroids_proj)
    log.info(
        "Vecindad pre-computada: grado medio %.2f (%.2f s)",
        adj.mean_degree,
        time.perf_counter() - t1,
    )
    t2 = time.perf_counter()
    try:
        transit = tr.load_transit()
        log.info(
            "Transporte masivo: %d estaciones (%d BRT, %d Cablebús), %.1f KB (%.2f s)",
            transit.n_stations,
            transit.counts["brt"],
            transit.counts["cable"],
            len(transit.geojson) / 1024,
            time.perf_counter() - t2,
        )
    except Exception as exc:  # la app sigue siendo útil sin las capas de referencia
        log.warning("No se pudieron cargar las capas de transporte: %s", exc)
        transit = None

    STATE["mesh"] = mesh
    STATE["adj"] = adj
    STATE["transit"] = transit
    yield
    STATE.clear()


app = FastAPI(title="SBC Puebla — Explorador de polígonos piloto", lifespan=lifespan)
# El GeoJSON de la malla comprime ~10:1; imprescindible para los 16k hexágonos.
app.add_middleware(GZipMiddleware, minimum_size=1024)


class PolygonRequest(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    density: int = Field(
        10, ge=alg.DENSITY_MIN, le=alg.DENSITY_MAX, description="estaciones por km²"
    )
    stations: int = Field(
        alg.STATIONS_TOTAL, ge=alg.STATIONS_MIN, le=alg.STATIONS_MAX,
        description="estaciones del piloto",
    )
    lambda_val: float = Field(
        alg.DEFAULT_LAMBDA, ge=0.0, le=1.0,
        description="Peso de la penalización por distancia (0=solo score, 1=máxima compacidad)",
    )


@app.get("/data")
def get_mesh_data() -> Response:
    """GeoJSON completo de la malla en WGS84 (id, Prioridad, cx, cy)."""
    mesh: dl.MeshData = STATE["mesh"]
    return Response(content=mesh.mesh_geojson, media_type="application/json")


@app.get("/transit")
def get_transit() -> Response:
    """Troncales RUTA (BRT) y Cablebús + sus estaciones, en WGS84."""
    transit: tr.TransitData | None = STATE.get("transit")
    if transit is None:
        return Response(
            content=b'{"lines":{"type":"FeatureCollection","features":[]},'
            b'"stations":{"type":"FeatureCollection","features":[]},'
            b'"counts":{"brt":0,"cable":0}}',
            media_type="application/json",
        )
    return Response(content=transit.geojson, media_type="application/json")


@app.get("/meta")
def get_meta() -> dict:
    """Metadatos para inicializar el frontend."""
    mesh: dl.MeshData = STATE["mesh"]
    minx, miny, maxx, maxy = mesh.bounds_wgs84
    return {
        "n_hexes": mesh.n_hexes,
        "hex_area_km2": round(mesh.hex_area_km2, 5),
        "score_min": round(float(mesh.scores.min()), 3),
        "score_max": round(float(mesh.scores.max()), 3),
        "score_mean": round(float(mesh.scores.mean()), 4),
        "histogram": mesh.score_histogram,
        "bounds": {"west": minx, "south": miny, "east": maxx, "north": maxy},
        "stations_total": alg.STATIONS_TOTAL,
        "hex_area_nominal": alg.HEX_AREA_KM2_NOMINAL,
        "density_range": [alg.DENSITY_MIN, alg.DENSITY_MAX],
        "stations_range": [alg.STATIONS_MIN, alg.STATIONS_MAX],
        "densities": [
            {"density": d, "hexes": n, "area_km2": round(n * mesh.hex_area_km2, 2)}
            for d, n in sorted(alg.DENSITY_TARGETS.items())
        ],
        # `levels` son los 3 valores que puede tomar cada variable: el dato
        # crudo es 0, 1 o 2, y V = P * (max/2). El frontend colorea con eso.
        "variables": [
            {
                "key": k,
                "label": dl.VAR_LABELS[k],
                "max": dl.VAR_MAX[k],
                "levels": [0.0, dl.VAR_MAX[k] / 2, dl.VAR_MAX[k]],
                "counts": [
                    int((mesh.raw_scores[:, j] == lvl).sum()) for lvl in (0, 1, 2)
                ],
            }
            for j, k in enumerate(dl.VAR_COLS)
        ],
        "transit": (
            {"counts": STATE["transit"].counts, "labels": tr.MODE_LABELS}
            if STATE.get("transit")
            else None
        ),
    }


@app.post("/polygon")
def post_polygon(req: PolygonRequest) -> dict:
    """Construye el polígono piloto óptimo alrededor del punto dado."""
    mesh: dl.MeshData = STATE["mesh"]
    adj: alg.Adjacency = STATE["adj"]
    t0 = time.perf_counter()
    result = alg.build_polygon(
        mesh, adj, req.lat, req.lon, req.density,
        lambda_val=req.lambda_val, transit=STATE.get("transit"),
        stations=req.stations,
    )
    result["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    log.info(
        "/polygon d=%d est=%d λ=%.2f -> %d hexes, %.2f km², score μ=%.3f, compac=%.3f (%.0f ms)",
        req.density,
        req.stations,
        req.lambda_val,
        result["metrics"]["n_hexes"],
        result["metrics"]["area_km2"],
        result["metrics"]["score_mean"],
        result["metrics"]["compactness"],
        result["elapsed_ms"],
    )
    return result


@app.get("/hex/{hex_id}")
def get_hex(hex_id: int) -> dict:
    """Perfil completo de un hexágono (las 7 variables) para el panel lateral."""
    mesh: dl.MeshData = STATE["mesh"]
    try:
        return dl.hex_profile(mesh, hex_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Hexágono {hex_id} no encontrado")


@app.get("/")
def index() -> FileResponse:
    # no-store: este archivo se edita en cada iteración de desarrollo local;
    # sin esto Chrome sirve una copia cacheada aunque el archivo cambie.
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
