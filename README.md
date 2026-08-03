---
title: SBC Puebla — Explorador de polígonos piloto
emoji: 🚲
colorFrom: blue
colorTo: red
sdk: docker
app_port: 7860
pinned: false
---

<!-- El bloque YAML de arriba solo lo lee Hugging Face Spaces para configurar
el despliegue (ver "Desplegar en línea" más abajo); no afecta el uso local. -->

# Explorador de polígonos piloto — SBC Puebla

Herramienta web local para delimitar el polígono de un piloto de Sistema de
Bicicletas Compartidas sobre la malla hexagonal de priorización de la zona
conurbada de Puebla.

## Arranque

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn main:app --port 8000
```

Abrir <http://127.0.0.1:8000>. La malla se lee y se serializa una sola vez al
arranque (~0.5 s); no se vuelve a tocar el GeoPackage en ningún request.

Pruebas: `.venv/bin/python test_algorithm.py`

## Archivos

| Archivo | Rol |
|---|---|
| `data_loader.py` | Lee `Malla 2.gpkg`, reproyecta, simplifica y cachea el GeoJSON |
| `algorithm.py` | KDTree de vecinos + expansión greedy contigua + métricas |
| `transit.py` | Troncales RUTA y Cablebús: clasificación, cacheo y conteo |
| `main.py` | FastAPI: `/data`, `/meta`, `/transit`, `/polygon`, `/hex/{id}` |
| `static/index.html` | Frontend completo (Leaflet + D3, sin build) |
| `test_algorithm.py` | 94 verificaciones: datos, vecindad, algoritmo, λ, forma, rangos y transporte |

## Dataset

`Malla 2.gpkg`, capa `malla_prueba_2` — **ojo: el archivo tiene un espacio en
el nombre**, no un guion bajo. `data_loader.py` acepta ambas variantes.

- 16,411 hexágonos, EPSG:6657, spacing de centroides 250.0 m exactos
- 0.05413 km² por hexágono
- `Prioridad` ∈ [0, 1.9] = suma exacta de las 7 columnas `V`
- Pesos máximos: RUTA 0.5 · Empleo 0.4 · Ciclista 0.3 · Densidad / AVerde /
  Turístico / Escuelas 0.2 cada una
- **No hay columna de municipio.** El tooltip muestra `Prioridad`, el id, el
  área y el percentil del hexágono dentro de la malla.

## Algoritmo

Al arranque se construye la adyacencia con `cKDTree.query_pairs(260 m)`:
48,254 pares, grado 6 en 15,437 de los 16,411 hexágonos (el resto son borde).

Por cada request:

1. **Semilla** — mayor `Prioridad` dentro de 500 m del punto. Si no hay nada,
   amplía a 1,000 / 2,500 / 5,000 m y lo reporta como warning.
2. **Expansión** — heap de frontera; en cada iteración entra el candidato de
   mayor **score efectivo**:

   ```
   score_efectivo = Prioridad − λ · (dist_al_centroide / 2500 m)
   ```

   donde `dist_al_centroide` es la distancia del hexágono candidato al punto
   que arrastra el usuario (no a la semilla) y 2,500 m es el radio de
   búsqueda de referencia del dataset. `λ` es ajustable por escenario desde
   el slider "Compacidad" del panel (`lambda_val` en `POST /polygon`,
   rango 0–1, default 0.3). En λ=0 el criterio es Prioridad pura
   (comportamiento original); λ crecientes penalizan alejarse del centroide
   y producen polígonos más redondos. Empates residuales se rompen por esa
   misma distancia.
3. **Corte** al alcanzar el objetivo, `round(estaciones / densidad / 0.0541)`.
   Con las 137 del sistema completo reproduce los objetivos del estudio: 281
   (9/km²), 253 (10/km²) y 230 (11/km²). Ambos parámetros son ajustables:
   **densidad 7–12 est/km²** y **estaciones 100–150**, lo que abarca de 154 a
   396 hexágonos (8.3 a 21.4 km²).
4. **Saneamiento de la forma** (ver abajo).
5. **Disolución** — `union_all` de los miembros en EPSG:6657.

### Saneamiento de la forma

El greedy puro deja dos patologías que no son operables en calle: **huecos**
interiores (bolsillos de baja prioridad que nunca se seleccionan pero quedan
rodeados) y **brazos de un hexágono de ancho**. λ no las resuelve porque es una
presión global de forma, mientras que ambas son locales y topológicas: en el
Centro, incluso con λ=1.0 quedaban 1 hueco y 8 celdas colgando de 2 lados.

Se atacan por dos vías simultáneas:

- **Al crecer**, el bono de cohesión `β=0.35` hace que el frente rellene
  concavidades antes de estirar tentáculos.
- **Al terminar**, un ciclo de rellenar huecos → podar celdas unidas por menos
  de 3 lados → reajustar al conteo exacto, iterado hasta que sea estable. Podar
  por debajo de 3 contactos fija el **grosor mínimo en 2 hexágonos (~500 m)**.
  Cuando el conteo exacto obliga a dejar una celda mal encajada, se intenta un
  intercambio neutro en conteo: rellenar la muesca junto a ella y soltar el
  hexágono de menor valor de un tramo grueso del borde.

Medido sobre **200 centroides aleatorios** con densidades y λ variados:

| | greedy puro (antes) | con saneamiento |
|---|---|---|
| Huecos | 3 (Centro, λ=0.2) | **0 en los 200** |
| Fragmentos | — | **0 en los 200** |
| Conteo exacto | sí | **200/200** |
| Celdas con ≤2 contactos | 19 (Centro, λ=0.2) | 4 en 50,600 hexágonos |
| Compacidad (Centro, λ=0.2) | 0.114 | **0.367** |
| Score μ (Centro, λ=0.2) | 1.535 | 1.494 (−2.7%) |

El costo es real y no se oculta: rellenar un hueco obliga a **incluir
hexágonos de baja prioridad** (el score mínimo baja de 1.25 a ~1.00). Es
inevitable — no se puede operar una dona. Las métricas `holes`, `parts` y
`thin_hexes` viajan en la respuesta y el panel avisa si alguna no es cero.

Las celdas residuales con 2 contactos son un solo hexágono de borde y aparecen
en ~2% de los centroides: exigir un conteo **exacto** lo hace geométricamente
inevitable, porque la última celda que entra a un borde ya liso solo puede
tocar por 2 lados. Mover un poco el centroide lo resuelve.

La contigüidad está garantizada por construcción: un hexágono solo puede entrar
si ya es vecino del cluster. El test compara contra `top-N por score` dentro de
2,500 m y confirma que ese enfoque produce fragmentos desconectados.

λ sigue mandando sobre la forma por encima del saneamiento. Medido en Cholula
(19.058, -98.302) a 10 est/km², ya con el saneamiento activo: la compacidad
Polsby-Popper sube de **0.199 (λ=0) a 0.348 (λ=1.0)** a costa de bajar el score
promedio de 0.658 a 0.594 — el trade-off es real y visible en la tabla de
comparación de escenarios.

## Rendimiento medido

| | |
|---|---|
| Carga de la malla + vecindad | 0.51 s (una vez, al arranque) |
| `GET /data` | 5.02 MB → **694 KB** con gzip (incluye las 7 variables) |
| `POST /polygon` | **16–190 ms** con saneamiento de forma (presupuesto: 2,000 ms) |

Los 16k hexágonos se dibujan con el renderer **canvas** de Leaflet; el
hit-testing de hover/click no usa capas interactivas sino un `d3.quadtree`
sobre los centroides, porque 16k polígonos interactivos hacen inviable el
hit-testing nativo.

## Capas de transporte masivo

Insumo: `insumosAPP/Rutas_Estaciones.zip` (se descomprime solo al arranque).

- **7 troncales** con atributo `Tipo`: 5 de BRT (L1–L4) y 2 tramos de
  Teleférico. No hubo que inferir nada, el dato ya venía tipificado.
- **162 estaciones** sin atributo de tipo. Se clasifican de forma
  determinista: las de BRT declaran `Linea` (1–4) y/o `layer` con el nombre
  de la troncal; las **9 restantes** tienen ambos campos nulos. Esas 9 caen a
  menos de 1 m de la traza del teleférico y sus nombres coinciden uno a uno
  con las 9 estaciones anunciadas del Cablebús (Parque Juárez, CENHCH,
  Parque Ecológico, Xonaca, Parque Biblioteca, Cerro Amalucan, Unidad
  Deportiva, Mayorazgo, CIS).
- El `.prj` de las estaciones es un CRS **compuesto** (UTM + altura) que
  geopandas no resuelve. El horizontal correcto es **EPSG:32614**: alinea
  cada estación a menos de 17 m de su traza, mientras que las variantes NAD83
  fallan por cientos de kilómetros. El test lo verifica en cada corrida.
- `POST /polygon` devuelve `stations_brt`, `stations_cable` y
  `stations_transit` — cuántas estaciones de cada modo caen dentro del
  polígono construido. Aparecen como métricas y como filas de la tabla
  comparativa de escenarios.

## Mapas base

Cinco opciones seleccionables: **Claro** y **Oscuro** (CARTO, neutros para
leer el choropleth), **Calles y negocios** (OpenStreetMap: comercios y puntos
de interés), **Colonias** (CARTO Voyager: calles, nombres y zonas sin
saturar) y **Satélite** (Esri World Imagery con etiquetas encima).

**Sobre Google Maps:** sus teselas no se pueden consumir desde un `TileLayer`
de Leaflet — requieren su propio SDK con una llave de facturación activa, y
extraerlas directamente viola sus términos de servicio. "Calles y negocios"
(OSM) es el equivalente libre para identificar comercios y puntos de
interés, y "Colonias" (Voyager) cubre el segundo caso que pediste.

El **slider de opacidad** atenúa la malla completa para leer las referencias
por debajo. Está implementado sobre la opacidad del `<canvas>` (compositing
por GPU), no sobre el estilo de cada polígono, así que se arrastra en vivo sin
repintar los 16k hexágonos.

## Decisiones de color

- **Choropleth**: 6 clases discretas y fijas sobre el valor real de
  `Prioridad` (no una rampa continua): gris `#D9D9D9` (= 0), azul `#2b83ba`
  (0–1.0), verde `#abdda4` (1.0–1.2), amarillo `#ffffbf` (1.2–1.4), naranja
  `#fdae61` (1.4–1.6) y rojo `#d7191c` (≥ 1.6). Los intervalos son cerrados
  por la izquierda, de modo que `Prioridad` exactamente 1.0 entra en "baja"
  (la definición original dejaba ese valor sin clase). Los colores son
  literales e iguales en ambos temas — es la clasificación cartográfica del
  dato, no un token de UI. Borde blanco 0.5px al 30% de opacidad. El
  histograma de la leyenda usa la misma clasificación, coloreando cada bin
  por su **borde izquierdo** (no el punto medio) para que el bin dominado por
  ceros exactos lea gris y no azul.
- **Vista por variable**: el selector encima de la leyenda cambia el
  choropleth entre el score total y cada una de las 7 variables. Cada variable
  individual **solo toma 3 valores** (el puntaje crudo es 0, 1 o 2), así que se
  clasifica en 3 clases reales — gris `#D9D9D9`, naranja `#fdae61` y rojo
  `#d7191c`, reusando el esquema del score — en vez de reescalar las 6 clases,
  que dejaría la mitad de la leyenda siempre vacía. El histograma se sustituye
  por 3 barras con el conteo por nivel, y el filtro por rango se oculta porque
  describe el score total.

  `GET /data` incluye ahora el campo `v`: los 7 puntajes crudos como enteros.
  Se mandan esos y no los valores ponderados porque **`V = P × (peso/2)` es
  exacto** en las siete variables (error 0 en los 16,411 hexágonos), así que el
  frontend los reconstruye sin pérdida y el payload comprimido sube solo de
  689 a 694 KB — los ceros, que son el 72–96% de cada variable, comprimen casi
  perfecto.
- **Transporte masivo**: el choropleth ya consume azul, verde, amarillo,
  naranja y rojo, así que la distinción entre modos no puede apoyarse solo en
  el hue. RUTA va en negro `#111111`, línea continua y estaciones circulares;
  el Cablebús en morado `#7b1fa2`, línea discontinua y estaciones cuadradas.
  Ambos llevan orla blanca de 6px por debajo para leerse igual sobre el
  satélite que sobre cualquier clase del choropleth.
- **Escenarios**: slots categóricos 2/3/7 — naranja `#eb6834`, aqua `#1baf7a`,
  violeta `#4a3aa7`. El trío se eligió enumerando las 35 combinaciones
  posibles y validando con el criterio all-pairs (obligatorio en formas tipo
  mapa, donde cualquier par de marcas puede quedar contiguo): **CVD ΔE 9.2
  claro / 9.4 oscuro**, por encima del target de 8.
- En tema claro el aqua queda en 2.74:1 contra la superficie (bajo 3:1); la
  compensación son las etiquetas directas de cada escenario y la tabla de
  comparación, así que la identidad nunca depende solo del color.
- Los charts de una sola serie (desglose por variable, perfil de hexágono)
  usan el hue fijo `--series-1` (azul) — no re-codifican magnitud con color
  porque la longitud de la barra ya la muestra.

## Advertencia sobre la forma de los polígonos

El algoritmo especificado maximiza el score y **no penaliza la forma**. En la
práctica esto produce polígonos alargados: en el Centro Histórico la compacidad
Polsby-Popper sale entre **0.08 y 0.13** (1.0 = círculo), porque la expansión
sigue los corredores de RUTA y de infraestructura ciclista en lugar de formar
una mancha redondeada.

El score promedio resultante es alto y correcto (≈1.53 contra 0.217 de la malla
completa), pero un área de servicio con esa geometría es cara de operar: mucho
perímetro (37–48 km), rebalanceo largo y peor accesibilidad peatonal entre
extremos. La app muestra la compacidad como métrica y emite un aviso cuando
baja de 0.20, para que la decisión sea explícita.

Esto ya no es una limitación abierta: el slider **Compacidad** (λ) del panel
lateral implementa exactamente esa penalización — ver la sección Algoritmo.

## Interacción con hexágonos

- **Clic** en cualquier hexágono de la malla: abre un popup fijo de Leaflet
  (se cierra con su X o al hacer clic en otro hexágono) con `Prioridad` y el
  desglose de las 7 variables, y simultáneamente actualiza el perfil en el
  panel lateral — un solo `GET /hex/{id}`, sin duplicar la petición.
- No hay tooltip por hover: se quitó porque generaba ruido visual al recorrer
  el mapa. Los tooltips de `pointermove` en los charts de barras (desglose y
  perfil) son independientes y se mantienen.
