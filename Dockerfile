# geopandas/shapely/pyogrio publican wheels con GDAL y GEOS ya enlazados
# estáticamente (manylinux); por eso no hace falta apt-get de libgdal-dev ni
# libgeos-dev — una imagen slim + pip basta. Verificado: la venv local instaló
# estos mismos paquetes sin ningún paso de sistema adicional.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Se extrae en el build, no en el arranque: algunas plataformas gratuitas
# (Hugging Face Spaces, Cloud Run) corren el contenedor con filesystem
# de solo lectura fuera de /tmp. transit.py ya no necesita escribir nada
# porque el shapefile de destino existe desde antes de que el proceso arranque.
RUN python -c "import zipfile; zipfile.ZipFile('insumosAPP/Rutas_Estaciones.zip').extractall('insumosAPP/Rutas_Estaciones')"

# 7860 es el puerto por defecto de Hugging Face Spaces (declarado como
# app_port en el frontmatter de README.md). Render y otras plataformas
# inyectan su propio $PORT como variable de entorno al arrancar el
# contenedor, y eso pisa este ENV automáticamente — no hace falta tocar
# nada para que funcione en ambas.
ENV PORT=7860
EXPOSE 7860

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
