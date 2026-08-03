"""Capas de referencia de transporte masivo: troncales RUTA (BRT) y Cablebús.

Insumos en `insumosAPP/Rutas_Estaciones/` (shapefiles):
  - "Troncales Puebla.shp"  — 7 líneas, con atributo `Tipo` (BRT | Teleférico)
  - "Estaciones Ruta.shp"   — 162 estaciones (sin atributo de tipo)

Las estaciones no traen tipo, pero sí se pueden clasificar de forma
determinista: las de BRT llevan `Linea` (1–4) y/o `layer` con el nombre de la
troncal; las 9 restantes tienen ambos nulos y caen a menos de 1 m de la traza
del teleférico. Esa clasificación coincide con las 9 estaciones anunciadas
para el Cablebús (Parque Juárez, CENHCH, Parque Ecológico, Xonaca, Parque
Biblioteca, Cerro Amalucan, Unidad Deportiva, Mayorazgo, CIS).
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd
import numpy as np

from data_loader import CRS_SOURCE, CRS_WGS84

BASE_DIR = Path(__file__).resolve().parent
INSUMOS_DIR = BASE_DIR / "insumosAPP"
SHP_DIR = INSUMOS_DIR / "Rutas_Estaciones"
ZIP_PATH = INSUMOS_DIR / "Rutas_Estaciones.zip"

LINES_SHP = "Troncales Puebla.shp"
STATIONS_SHP = "Estaciones Ruta.shp"

# El .prj de las estaciones es un CRS compuesto (UTM + altura) que geopandas no
# resuelve; EPSG:32614 es el horizontal correcto — verificado porque alinea las
# estaciones a <1 m de sus trazas (las alternativas NAD83 fallan por cientos de km).
STATIONS_CRS = 32614

MODE_BRT = "brt"
MODE_CABLE = "cable"
MODE_LABELS = {MODE_BRT: "RUTA (BRT)", MODE_CABLE: "Cablebús"}

COORD_PRECISION = 6


def _ensure_extracted() -> None:
    """Descomprime el zip de insumos la primera vez."""
    if (SHP_DIR / LINES_SHP).exists():
        return
    if not ZIP_PATH.exists():
        raise FileNotFoundError(f"No encontré {ZIP_PATH} ni {SHP_DIR / LINES_SHP}")
    with zipfile.ZipFile(ZIP_PATH) as zf:
        zf.extractall(SHP_DIR.parent / SHP_DIR.name if SHP_DIR.exists() else SHP_DIR)


def _round_coords(obj):
    """Redondea recursivamente las coordenadas de un dict GeoJSON."""
    if isinstance(obj, (int, float)):
        return round(obj, COORD_PRECISION)
    if isinstance(obj, (list, tuple)):
        return [_round_coords(v) for v in obj]
    return obj


@dataclass
class TransitData:
    """Líneas y estaciones de transporte masivo, listas para servir y contar."""

    geojson: bytes                        # payload cacheado de GET /transit
    station_xy: np.ndarray                # (N, 2) en EPSG:6657, para point-in-polygon
    station_mode: np.ndarray              # (N,) "brt" | "cable"
    station_name: list[str]
    station_line: list[str]
    counts: dict[str, int]

    @property
    def n_stations(self) -> int:
        return len(self.station_xy)


def _classify_station(row) -> str:
    """BRT si declara línea troncal; Cablebús si no declara ninguna."""
    has_linea = row.get("Linea") is not None and not (
        isinstance(row.get("Linea"), float) and np.isnan(row.get("Linea"))
    )
    layer = row.get("layer")
    has_layer = isinstance(layer, str) and layer.strip() != ""
    return MODE_BRT if (has_linea or has_layer) else MODE_CABLE


def load_transit() -> TransitData:
    """Lee ambos shapefiles, clasifica, reproyecta y cachea el GeoJSON."""
    _ensure_extracted()

    # --- Líneas -------------------------------------------------------------
    lines = gpd.read_file(SHP_DIR / LINES_SHP)
    if lines.crs is None:
        lines = lines.set_crs(CRS_WGS84)
    lines_ll = lines.to_crs(CRS_WGS84)
    lines_proj = lines.to_crs(CRS_SOURCE)

    line_features = []
    for (_, row), geom_ll, geom_p in zip(
        lines.iterrows(), lines_ll.geometry, lines_proj.geometry
    ):
        tipo = str(row.get("Tipo") or "").strip()
        mode = MODE_CABLE if tipo.lower().startswith("telef") else MODE_BRT
        line_features.append(
            {
                "type": "Feature",
                "properties": {
                    "mode": mode,
                    "tipo": tipo,
                    "linea": str(row.get("Línea") or "").strip(),
                    "km": round(geom_p.length / 1000, 2),
                },
                "geometry": _round_coords(geom_ll.__geo_interface__),
            }
        )

    # --- Estaciones ---------------------------------------------------------
    st = gpd.read_file(SHP_DIR / STATIONS_SHP).set_crs(
        STATIONS_CRS, allow_override=True
    )
    st_ll = st.to_crs(CRS_WGS84)
    st_proj = st.to_crs(CRS_SOURCE)

    modes, names, line_labels, station_features = [], [], [], []
    for (_, row), geom_ll in zip(st.iterrows(), st_ll.geometry):
        mode = _classify_station(row)
        name = str(row.get("Name") or "").strip() or "s/n"
        if mode == MODE_BRT:
            linea = row.get("Linea")
            label = f"L{int(linea)}" if linea == linea and linea is not None else "RUTA"
        else:
            label = "Cablebús"
        modes.append(mode)
        names.append(name)
        line_labels.append(label)
        station_features.append(
            {
                "type": "Feature",
                "properties": {"mode": mode, "name": name, "line": label},
                "geometry": {
                    "type": "Point",
                    "coordinates": [
                        round(geom_ll.x, COORD_PRECISION),
                        round(geom_ll.y, COORD_PRECISION),
                    ],
                },
            }
        )

    station_xy = np.c_[
        st_proj.geometry.x.to_numpy(), st_proj.geometry.y.to_numpy()
    ]
    station_mode = np.array(modes, dtype=object)
    counts = {
        MODE_BRT: int((station_mode == MODE_BRT).sum()),
        MODE_CABLE: int((station_mode == MODE_CABLE).sum()),
    }

    payload = {
        "lines": {"type": "FeatureCollection", "features": line_features},
        "stations": {"type": "FeatureCollection", "features": station_features},
        "counts": counts,
        "labels": MODE_LABELS,
    }

    return TransitData(
        geojson=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        station_xy=station_xy,
        station_mode=station_mode,
        station_name=names,
        station_line=line_labels,
        counts=counts,
    )


def stations_within(transit: TransitData, geom_proj) -> dict:
    """Cuenta y lista las estaciones dentro de un polígono en EPSG:6657."""
    from shapely.geometry import Point
    from shapely.prepared import prep

    prepared = prep(geom_proj)
    inside = np.array(
        [prepared.contains(Point(x, y)) for x, y in transit.station_xy], dtype=bool
    )

    listing = [
        {
            "name": transit.station_name[i],
            "line": transit.station_line[i],
            "mode": transit.station_mode[i],
        }
        for i in np.flatnonzero(inside)
    ]
    return {
        "brt": int(((transit.station_mode == MODE_BRT) & inside).sum()),
        "cable": int(((transit.station_mode == MODE_CABLE) & inside).sum()),
        "total": int(inside.sum()),
        "stations": listing,
    }
