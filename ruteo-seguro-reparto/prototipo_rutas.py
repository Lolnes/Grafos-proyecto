# -*- coding: utf-8 -*-
"""
Prototipo: ruta de tiempo minimo para un repartidor de ultima milla,
sin pasar por los tramos criticos (el q% mas riesgoso) de la comuna de Santiago.

Modelo
    min  sum_e t_e x_e
    s.a. conservacion de flujo (o -> z)
         x_e = 0  para todo arco critico (I_e > 1 - q), salvo el primer y ultimo arco
         x_e en {0,1}
    I_e = max(P_vial(e), P_delictual(e))  (percentiles dentro de la comuna)

Como la restriccion de riesgo solo elimina arcos, el problema se resuelve con
Dijkstra sobre el grafo sin los arcos criticos.

Uso (en tu PC, con internet):
    pip install osmnx folium matplotlib scipy requests
    python prototipo_rutas.py
Si ya tienen los siniestros de CONASET en un CSV, dejenlo junto al script como
siniestros_santiago.csv con columnas lat, lon, hora (se usa en vez de descargar).

Prueba sin internet (grafo sintetico):
    python prototipo_rutas.py --demo

Salida: carpeta resultados/ (tablas .tex/.csv y figuras .png/.html) lista para
subir a Overleaf junto a main.tex.
"""
import argparse
import math
import os
import time

import numpy as np
import pandas as pd
import networkx as nx

# ----------------------------------------------------------------------------
# Parametros (todos los del modelo estan aqui)
# ----------------------------------------------------------------------------
LUGAR = "Santiago, Región Metropolitana de Santiago, Chile"
CRS_M = 32719                 # UTM 19S, metros
HORA_INI, HORA_FIN = 8, 21    # jornada de reparto (siniestros fuera de esta franja no se cuentan)
Q = 0.01                      # fraccion de tramos criticos que se evita (1 %, ver sensibilidad)
Q_SENS = [0.01, 0.025, 0.05, 0.10]
RADIO = 150                   # m, radio para contar locales y paraderos
SNAP = 25                     # m, distancia maxima siniestro -> tramo
N_PARES = 200                 # pares origen-destino para evaluar la situacion actual
DIST_MIN, DIST_MAX = 1000, 4000   # m en linea recta entre origen y destino
N_DIAS, N_PARADAS = 30, 25    # jornadas simuladas y entregas por jornada
SEMILLA = 7
VEL_DEFECTO = 30              # km/h si OSM no trae velocidad para ese tipo de via

# Coeficientes de literatura (factor por unidad de la variable)
COEF = {
    "betw_z": 1.19,     # Summers y Johnson (2017): por desviacion estandar de betweenness
    "locales_z": 1.20,  # Bernasco y Block (2011): se aplica por desviacion estandar (supuesto)
    "paraderos": 1.017, # Diaz et al. (2026): por paradero en el radio
}

# Ejemplo ilustrativo: dos entregas consecutivas de un repartidor que cruza el
# centro de la comuna, de Barrio Yungay (Plaza Yungay) a Barrio Lastarria
# (lon, lat)
EJEMPLO = {"nombre": "entrega en Barrio Yungay a entrega en Barrio Lastarria",
           "o": (-70.6631, -33.4372), "z": (-70.6400, -33.4377)}

URL_CONASET = ("https://services3.arcgis.com/vaJl1B5HEzZj7154/arcgis/rest/services/"
               "Base_SINIESTROS_2020_2025/FeatureServer/0/query")
OUT = "resultados"


# ----------------------------------------------------------------------------
# 1. Datos
# ----------------------------------------------------------------------------
SERVIDORES_OSM = ["https://overpass-api.de/api",
                  "https://overpass.kumi.systems/api",
                  "https://overpass.private.coffee/api"]


def con_reintentos(fn, *args, **kw):
    """Los servidores publicos de OSM a veces estan saturados: se reintenta
    alternando servidores y esperando cada vez mas. Lo ya descargado queda en
    la carpeta cache/ y no se vuelve a pedir."""
    import osmnx as ox
    espera = 10
    for intento in range(8):
        ox.settings.overpass_url = SERVIDORES_OSM[intento % len(SERVIDORES_OSM)]
        try:
            return fn(*args, **kw)
        except Exception as e:
            print(f"  servidor {ox.settings.overpass_url} no respondio ({type(e).__name__}); "
                  f"reintento en {espera} s...")
            time.sleep(espera)
            espera = min(espera * 2, 120)
    raise RuntimeError("No se pudo descargar desde OSM. Intenta de nuevo en unos minutos.")


def cargar_grafo():
    import osmnx as ox
    ox.settings.use_cache = True
    ox.settings.requests_timeout = 300
    print("Descargando red vial (OSM)...")
    G = con_reintentos(ox.graph_from_place, LUGAR, network_type="drive")
    G = ox.routing.add_edge_speeds(G, fallback=VEL_DEFECTO)
    G = ox.routing.add_edge_travel_times(G)
    poly = con_reintentos(ox.geocode_to_gdf, LUGAR).geometry.iloc[0]
    print("Descargando locales y paraderos (OSM)...")
    locales = con_reintentos(ox.features_from_polygon, poly, {"shop": True})
    paraderos = con_reintentos(ox.features_from_polygon, poly, {"highway": "bus_stop"})
    return G, poly, a_puntos(locales), a_puntos(paraderos)


def a_puntos(gdf):
    import geopandas as gpd
    gdf = gdf.to_crs(CRS_M)
    return gpd.GeoDataFrame(geometry=gdf.geometry.centroid, crs=CRS_M)


def cargar_siniestros(poly):
    import geopandas as gpd
    if os.path.exists("siniestros_santiago.csv"):
        print("Usando siniestros_santiago.csv local")
        df = pd.read_csv("siniestros_santiago.csv")
        df.columns = [c.lower() for c in df.columns]
    else:
        import requests
        print("Descargando siniestros CONASET 2020-2025 (ArcGIS)...")
        xmin, ymin, xmax, ymax = poly.bounds
        filas, off = [], 0
        while True:
            p = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "outSR": 4326,
                 "geometry": f"{xmin},{ymin},{xmax},{ymax}", "geometryType": "esriGeometryEnvelope",
                 "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
                 "resultOffset": off, "resultRecordCount": 2000, "f": "json"}
            r = requests.get(URL_CONASET, params=p, timeout=120).json()
            feats = r.get("features", [])
            for f in feats:
                a = f["attributes"]
                g = f.get("geometry") or {}
                filas.append({"lon": g.get("x"), "lat": g.get("y"), "hora": a.get("Hora_aprox")})
            if len(feats) < 2000:
                break
            off += 2000
        df = pd.DataFrame(filas)
    df["hora"] = pd.to_numeric(df["hora"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    total = len(df)
    df = df[(df["hora"] >= HORA_INI) & (df["hora"] <= HORA_FIN)]
    pts = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df["lon"], df["lat"]), crs=4326)
    pts = pts[pts.within(poly)].to_crs(CRS_M)
    print(f"  siniestros en la caja: {total}; en la comuna y en jornada {HORA_INI}-{HORA_FIN} h: {len(pts)}")
    return pts


# ----------------------------------------------------------------------------
# 2. Indice de riesgo por tramo
# ----------------------------------------------------------------------------
def tramos(G):
    """Un registro por tramo fisico (calle de doble sentido = un tramo)."""
    import osmnx as ox
    _, e = ox.convert.graph_to_gdfs(G)
    e = e.reset_index()
    e["tramo"] = [tuple(sorted((u, v))) for u, v in zip(e["u"], e["v"])]
    t = e.sort_values("length").drop_duplicates("tramo")[["tramo", "length", "geometry"]]
    return t.set_crs(4326, allow_override=True).to_crs(CRS_M).reset_index(drop=True)


def contar_en_radio(t, pts, radio):
    if len(pts) == 0:
        return np.zeros(len(t))
    mid = t.geometry.interpolate(0.5, normalized=True)
    tree = pts.sindex
    idx = tree.query(mid.buffer(radio), predicate="intersects")
    return np.bincount(idx[0], minlength=len(t))


def indice_delictual(t, coef):
    z = lambda s: (s - s.mean()) / (s.std() or 1)
    ln_s = (math.log(coef["betw_z"]) * z(t["betw"])
            + math.log(coef["locales_z"]) * z(t["locales"])
            + math.log(coef["paraderos"]) * t["paraderos"])
    return np.exp(ln_s - ln_s.mean())        # 1 = tramo promedio


def robustez(t):
    """Varia cada coeficiente (efecto en escala logaritmica x0,5 y x1,5) y mide
    que fraccion de los tramos criticos delictuales se mantiene."""
    base = set(t.index[t["P_del"] > 1 - Q])
    nombres = {"betw_z": "Betweenness", "locales_z": "Locales", "paraderos": "Paraderos"}
    filas = []
    for k in COEF:
        for f in (0.5, 1.5):
            c = dict(COEF); c[k] = math.exp(math.log(COEF[k]) * f)
            s = indice_delictual(t, c)
            nuevo = set(t.index[s.rank(pct=True, method="max") > 1 - Q])
            filas.append({"variable": nombres[k], "factor": f, "coef": c[k],
                          "se_mantiene_pct": 100 * len(base & nuevo) / max(len(base), 1)})
    return pd.DataFrame(filas)


def indice_riesgo(G, siniestros, locales, paraderos):
    import geopandas as gpd
    t = tramos(G)
    # Capa vial: siniestros (en jornada) pegados al tramo mas cercano
    if len(siniestros):
        j = gpd.sjoin_nearest(siniestros, t[["geometry"]], max_distance=SNAP, how="inner")
        t["n_sin"] = np.bincount(j["index_right"], minlength=len(t))
    else:
        t["n_sin"] = 0
    # Capa delictual: indice relativo con coeficientes de literatura
    D = nx.DiGraph()
    for u, v, d in G.edges(data=True):
        if not D.has_edge(u, v) or d["length"] < D[u][v]["length"]:
            D.add_edge(u, v, length=d["length"])
    k = min(1000, D.number_of_nodes())
    eb = nx.edge_betweenness_centrality(D, k=k, weight="length", seed=SEMILLA)
    bt = {}
    for (u, v), b in eb.items():
        key = tuple(sorted((u, v)))
        bt[key] = bt.get(key, 0) + b
    t["betw"] = t["tramo"].map(bt).fillna(0)
    t["locales"] = contar_en_radio(t, locales, RADIO)
    t["paraderos"] = contar_en_radio(t, paraderos, RADIO)
    t["s"] = indice_delictual(t, COEF)
    # Percentiles dentro de la comuna (0 = menos riesgoso, 1 = mas riesgoso)
    t["P_vial"] = t["n_sin"].rank(pct=True, method="max").where(t["n_sin"] > 0, 0.0)
    t["P_del"] = t["s"].rank(pct=True, method="max")
    t["I"] = t[["P_vial", "P_del"]].max(axis=1)
    return t


def marcar_grafo(G, t):
    m = t.set_index("tramo")
    for u, v, k, d in G.edges(keys=True, data=True):
        r = m.loc[[tuple(sorted((u, v)))]].iloc[0]
        d["n_sin"], d["s"], d["I"] = int(r["n_sin"]), float(r["s"]), float(r["I"])
        d["P_vial"], d["P_del"] = float(r["P_vial"]), float(r["P_del"])


# ----------------------------------------------------------------------------
# 3. Modelo: tiempo minimo sin arcos criticos
# ----------------------------------------------------------------------------
def ruta(G, o, z, q):
    """q = 0 -> ruta rapida (navegador actual). q > 0 -> ruta segura."""
    umbral = 1 - q

    def w(u, v, datos):
        mejor = None
        for d in datos.values():
            if q > 0 and d["I"] > umbral and u != o and v != z:
                continue
            mejor = d["travel_time"] if mejor is None else min(mejor, d["travel_time"])
        return mejor

    try:
        return nx.shortest_path(G, o, z, weight=w)
    except nx.NetworkXNoPath:
        return None


def medir(G, camino, q=Q):
    tt = lg = sin = crit = expd = 0.0
    for u, v in zip(camino[:-1], camino[1:]):
        d = min(G[u][v].values(), key=lambda a: a["travel_time"])
        tt += d["travel_time"]; lg += d["length"]; sin += d["n_sin"]
        expd += d["s"] * d["length"]
        crit += d["I"] > 1 - q
    return {"tiempo_s": tt, "largo_m": lg, "siniestros": sin, "exp_del": expd / 1000, "criticos": crit}


def pares_od(G, n):
    rng = np.random.default_rng(SEMILLA)
    nodos = list(G.nodes)
    xy = {k: (G.nodes[k]["x"], G.nodes[k]["y"]) for k in nodos}
    out = []
    while len(out) < n:
        o, z = rng.choice(nodos, 2, replace=False)
        dx = (xy[o][0] - xy[z][0]) * 111320 * math.cos(math.radians(xy[o][1]))
        dy = (xy[o][1] - xy[z][1]) * 110540
        if DIST_MIN <= math.hypot(dx, dy) <= DIST_MAX:
            out.append((o, z))
    return out


def evaluar(G):
    filas = []
    pares = pares_od(G, N_PARES)
    for i, (o, z) in enumerate(pares):
        base = ruta(G, o, z, 0)
        if base is None:
            continue
        for q in [0] + Q_SENS:
            c = base if q == 0 else ruta(G, o, z, q)
            if c is None:
                filas.append({"par": i, "q": q, "factible": False}); continue
            m = medir(G, c, q if q > 0 else Q)
            m_base = medir(G, base, q if q > 0 else Q)
            filas.append({"par": i, "q": q, "factible": True, **m,
                          "criticos_rapida": m_base["criticos"],
                          "cambia": c != base})
    return pd.DataFrame(filas)


def jornada(G):
    """Simula N_DIAS jornadas de N_PARADAS entregas. El orden de las paradas lo
    fija la 'empresa' (vecino mas cercano en linea recta, sin mirar el riesgo);
    cada tramo entre paradas se resuelve con la ruta rapida y con la segura."""
    rng = np.random.default_rng(SEMILLA + 1)
    nodos = list(G.nodes)
    filas = []
    for dia in range(N_DIAS):
        paradas = list(rng.choice(nodos, N_PARADAS, replace=False))
        orden = [paradas.pop(0)]
        while paradas:
            x0, y0 = G.nodes[orden[-1]]["x"], G.nodes[orden[-1]]["y"]
            k = min(range(len(paradas)), key=lambda i: (G.nodes[paradas[i]]["x"] - x0) ** 2
                    + (G.nodes[paradas[i]]["y"] - y0) ** 2)
            orden.append(paradas.pop(k))
        tot = {"r": {"tiempo_s": 0, "siniestros": 0, "exp_del": 0, "criticos": 0},
               "s": {"tiempo_s": 0, "siniestros": 0, "exp_del": 0, "criticos": 0}}
        sin_ruta = tramos_ok = 0
        for o, z in zip(orden[:-1], orden[1:]):
            cr = ruta(G, o, z, 0)
            if cr is None:
                continue
            cs = ruta(G, o, z, Q)
            if cs is None:            # sin alternativa: se usa la rapida y se avisa
                cs = cr; sin_ruta += 1
            tramos_ok += 1
            for key, c in (("r", cr), ("s", cs)):
                m = medir(G, c)
                for v in tot[key]:
                    tot[key][v] += m[v]
        filas.append({"dia": dia, "tramos": tramos_ok, "sin_alternativa": sin_ruta,
                      **{f"{v}_rapida": tot["r"][v] for v in tot["r"]},
                      **{f"{v}_segura": tot["s"][v] for v in tot["s"]}})
    return pd.DataFrame(filas)


def resumir(df):
    base = df[df.q == 0].set_index("par")
    filas = []
    for q in Q_SENS:
        d = df[(df.q == q) & df.factible].set_index("par")
        b = base.loc[d.index]
        filas.append({
            "q": q,
            "pct_rapida_cruza": 100 * (d["criticos_rapida"] > 0).mean(),
            "pct_cambia": 100 * d["cambia"].mean(),
            "aumento_tiempo_pct": 100 * (d["tiempo_s"] / b["tiempo_s"] - 1).mean(),
            "aumento_tiempo_s": (d["tiempo_s"] - b["tiempo_s"]).mean(),
            "red_siniestros_pct": 100 * (1 - d["siniestros"].sum() / max(b["siniestros"].sum(), 1e-9)),
            "red_exp_del_pct": 100 * (1 - d["exp_del"].sum() / b["exp_del"].sum()),
            "pct_factible": 100 * len(d) / len(base),
        })
    return pd.DataFrame(filas)


# ----------------------------------------------------------------------------
# 4. Salidas para el informe
# ----------------------------------------------------------------------------
def fmt(x, dec=1):
    return f"{x:,.{dec}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def escribir_tex_extra(jor, rob):
    j = jor.sum()
    dias = len(jor)
    c = {
        "resJorDias": fmt(dias, 0),
        "resJorParadas": fmt(N_PARADAS, 0),
        "resJorMinR": fmt(j["tiempo_s_rapida"] / dias / 60),
        "resJorMinS": fmt(j["tiempo_s_segura"] / dias / 60),
        "resJorMinExtra": fmt((j["tiempo_s_segura"] - j["tiempo_s_rapida"]) / dias / 60),
        "resJorPctExtra": fmt(100 * (j["tiempo_s_segura"] / j["tiempo_s_rapida"] - 1)),
        "resJorCritR": fmt(j["criticos_rapida"] / dias),
        "resJorCritS": fmt(j["criticos_segura"] / dias),
        "resJorRedSin": fmt(100 * (1 - j["siniestros_segura"] / max(j["siniestros_rapida"], 1e-9))),
        "resJorRedDel": fmt(100 * (1 - j["exp_del_segura"] / j["exp_del_rapida"])),
        "resJorSinAlt": fmt(100 * j["sin_alternativa"] / max(j["tramos"], 1)),
        "resRobMin": fmt(rob["se_mantiene_pct"].min(), 0),
        "resRobMedia": fmt(rob["se_mantiene_pct"].mean(), 0),
    }
    with open(os.path.join(OUT, "cifras_extra.tex"), "w", encoding="utf-8") as f:
        for k, v in c.items():
            f.write(f"\\newcommand{{\\{k}}}{{{v}}}\n")
    with open(os.path.join(OUT, "tabla_robustez.tex"), "w", encoding="utf-8") as f:
        f.write("\\begin{tabular}{lccc}\n\\toprule\n")
        f.write("Variable & Efecto & Factor usado & Tramos críticos que se mantienen \\\\\n\\midrule\n")
        for _, r in rob.iterrows():
            ef = "mitad" if r.factor == 0.5 else "1,5 veces"
            f.write(f"{r.variable} & {ef} & {fmt(r.coef, 3)} & {fmt(r.se_mantiene_pct, 0)}\\,\\% \\\\\n")
        f.write("\\bottomrule\n\\end{tabular}\n")


def escribir_tex(res, t, df, ejs):
    fila = res[res.q == Q].iloc[0]
    n_par = df[df.q == 0]["par"].nunique()
    with open(os.path.join(OUT, "tabla_sensibilidad.tex"), "w", encoding="utf-8") as f:
        f.write("\\begin{tabular}{rrrrr}\n\\toprule\n")
        f.write("$q$ & Rutas que cambian & Aumento de tiempo & Menos siniestros & Menos exp. delictual \\\\\n\\midrule\n")
        for _, r in res.iterrows():
            f.write(f"{fmt(100*r.q)}\\,\\% & {fmt(r.pct_cambia)}\\,\\% & "
                    f"{fmt(r.aumento_tiempo_pct)}\\,\\% ({fmt(r.aumento_tiempo_s,0)} s) & "
                    f"{fmt(r.red_siniestros_pct)}\\,\\% & {fmt(r.red_exp_del_pct)}\\,\\% \\\\\n")
        f.write("\\bottomrule\n\\end{tabular}\n")
    crit = (t["I"] > 1 - Q)
    with open(os.path.join(OUT, "cifras.tex"), "w", encoding="utf-8") as f:
        c = {
            "resTramos": fmt(len(t), 0),
            "resTramosCriticos": fmt(int(crit.sum()), 0),
            "resKmCriticos": fmt(t.loc[crit, "length"].sum() / 1000),
            "resKmTotal": fmt(t["length"].sum() / 1000),
            "resSiniestros": fmt(int(t["n_sin"].sum()), 0),
            "resPctSinCriticos": fmt(100 * t.loc[t.P_vial > 1 - Q, "n_sin"].sum() / max(t["n_sin"].sum(), 1)),
            "resPares": fmt(n_par, 0),
            "resPctCruza": fmt(fila.pct_rapida_cruza),
            "resPctCambia": fmt(fila.pct_cambia),
            "resAumentoPct": fmt(fila.aumento_tiempo_pct),
            "resAumentoSeg": fmt(fila.aumento_tiempo_s, 0),
            "resRedSin": fmt(fila.red_siniestros_pct),
            "resRedDel": fmt(fila.red_exp_del_pct),
            "resFactible": fmt(fila.pct_factible),
        }
        # Ejemplo 1 -> resEj..., ejemplo 2 -> resEjB...
        for pref, ej in zip(["resEj", "resEjB"], ejs):
            if not ej:
                continue
            c.update({pref + "TiempoR": fmt(ej["r"]["tiempo_s"] / 60), pref + "TiempoS": fmt(ej["s"]["tiempo_s"] / 60),
                      pref + "LargoR": fmt(ej["r"]["largo_m"], 0), pref + "LargoS": fmt(ej["s"]["largo_m"], 0),
                      pref + "CritR": fmt(ej["r"]["criticos"], 0), pref + "CritS": fmt(ej["s"]["criticos"], 0),
                      pref + "SinR": fmt(ej["r"]["siniestros"], 0), pref + "SinS": fmt(ej["s"]["siniestros"], 0),
                      pref + "Nombre": ej["nombre"],
                      pref + "Igual": "1" if ej["cr"] == ej["cs"] else "0"})
        for k, v in c.items():
            f.write(f"\\newcommand{{\\{k}}}{{{v}}}\n")


def mapa_ruta(G, tt, crit, ej, archivo):
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(7, 7))
    tt.plot(ax=ax, color="#dddddd", linewidth=0.5)
    tt[crit].plot(ax=ax, color="#e8a0a0", linewidth=1.5)
    for c, col, lab, an in [(ej["cr"], "#555555", "Ruta rápida", 5), (ej["cs"], "#1a8f4a", "Ruta segura", 3)]:
        xs = [G.nodes[n]["x"] for n in c]; ys = [G.nodes[n]["y"] for n in c]
        ax.plot(xs, ys, color=col, linewidth=an, label=lab)
    o, z = ej["cr"][0], ej["cr"][-1]
    ax.plot(G.nodes[o]["x"], G.nodes[o]["y"], "o", color="black", markersize=9)
    ax.plot(G.nodes[z]["x"], G.nodes[z]["y"], "s", color="black", markersize=9)
    xs = [G.nodes[n]["x"] for n in ej["cr"] + ej["cs"]]
    ys = [G.nodes[n]["y"] for n in ej["cr"] + ej["cs"]]
    pad = 0.004
    ax.set_xlim(min(xs) - pad, max(xs) + pad); ax.set_ylim(min(ys) - pad, max(ys) + pad)
    ax.set_axis_off(); ax.legend(loc="lower left", frameon=False)
    fig.savefig(os.path.join(OUT, archivo + ".png"), dpi=200, bbox_inches="tight"); plt.close(fig)
    try:
        import folium
        m = folium.Map(location=[ys[0], xs[0]], zoom_start=15, tiles="OpenStreetMap")
        for c, col in [(ej["cr"], "gray"), (ej["cs"], "green")]:
            folium.PolyLine([(G.nodes[n]["y"], G.nodes[n]["x"]) for n in c], color=col, weight=6).add_to(m)
        m.save(os.path.join(OUT, archivo + ".html"))
    except ImportError:
        pass


def figuras(G, t, ejs):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    tt = t.to_crs(4326)
    crit = tt["I"] > 1 - Q
    fig, ax = plt.subplots(figsize=(7, 7))
    tt[~crit].plot(ax=ax, color="#bbbbbb", linewidth=0.6)
    tt[crit & (tt.P_vial > 1 - Q)].plot(ax=ax, color="#1f5aa6", linewidth=1.8, label="Crítico vial")
    tt[crit & (tt.P_del > 1 - Q)].plot(ax=ax, color="#c0392b", linewidth=1.8, label="Crítico delictual")
    ax.set_axis_off(); ax.legend(loc="lower left", frameon=False)
    fig.savefig(os.path.join(OUT, "mapa_criticos.png"), dpi=200, bbox_inches="tight"); plt.close(fig)
    for ej, archivo in zip(ejs, ["mapa_ejemplo", "mapa_ejemplo2"]):
        if ej:
            mapa_ruta(G, tt, crit, ej, archivo)


def nombre_calle(G, u, v):
    n = min(G[u][v].values(), key=lambda a: a["travel_time"]).get("name", "calle sin nombre")
    return n[0] if isinstance(n, list) else str(n)


def armar_ejemplo(G, o, z, nombre):
    cr = ruta(G, o, z, 0)
    cs = ruta(G, o, z, Q) if cr else None
    if not cr or not cs:
        return None
    return {"nombre": nombre, "cr": cr, "cs": cs, "r": medir(G, cr), "s": medir(G, cs)}


def ejemplos(G, df):
    """Ejemplo 1: par fijo (EJEMPLO). Ejemplo 2: el par de la muestra en que el
    modelo cambia la ruta con un aumento de tiempo mas cercano a la mediana."""
    out = []
    try:
        import osmnx as ox
        o = ox.distance.nearest_nodes(G, *EJEMPLO["o"])
        z = ox.distance.nearest_nodes(G, *EJEMPLO["z"])
        out.append(armar_ejemplo(G, o, z, EJEMPLO["nombre"]))
    except Exception as e:
        print("AVISO: no se pudo calcular el ejemplo 1:", e)
        out.append(None)
    base = df[df.q == 0].set_index("par")["tiempo_s"]
    d = df[(df.q == Q) & df.factible & df.cambia].copy()
    if d.empty:
        out.append(None)
        return out
    d["aum"] = d["tiempo_s"].values / base.loc[d["par"]].values - 1
    par = int(d.iloc[(d["aum"] - d["aum"].median()).abs().argsort().iloc[0]]["par"])
    o, z = pares_od(G, N_PARES)[par]
    cr = ruta(G, o, z, 0)
    nombre = f"desde {nombre_calle(G, cr[0], cr[1])} hasta {nombre_calle(G, cr[-2], cr[-1])}"
    out.append(armar_ejemplo(G, o, z, nombre))
    print("Ejemplo 2:", nombre)
    return out


# ----------------------------------------------------------------------------
# Modo demo (sin internet): grilla sintetica para probar el codigo
# ----------------------------------------------------------------------------
def demo():
    import geopandas as gpd
    from shapely.geometry import LineString, Point, box
    rng = np.random.default_rng(1)
    n, paso = 30, 110
    lon0, lat0 = -70.67, -33.46
    G = nx.MultiDiGraph(crs="epsg:4326")
    for i in range(n):
        for j in range(n):
            G.add_node(i * n + j, x=lon0 + j * paso / 92900, y=lat0 + i * paso / 110540)
    for i in range(n):
        for j in range(n):
            for di, dj in [(0, 1), (1, 0)]:
                if i + di < n and j + dj < n:
                    a, b = i * n + j, (i + di) * n + j + dj
                    vel = 50 if (i % 6 == 0 or j % 6 == 0) else 30
                    for u, v in [(a, b), (b, a)]:
                        geom = LineString([(G.nodes[u]["x"], G.nodes[u]["y"]), (G.nodes[v]["x"], G.nodes[v]["y"])])
                        G.add_edge(u, v, length=paso, speed_kph=vel, travel_time=paso / (vel / 3.6), geometry=geom)
    poly = box(lon0, lat0, lon0 + n * paso / 92900, lat0 + n * paso / 110540)
    def nube(k, cx, cy, sd):
        p = gpd.GeoDataFrame(geometry=[Point(x, y) for x, y in zip(rng.normal(cx, sd, k), rng.normal(cy, sd, k))], crs=4326)
        return p[p.within(poly)]
    cx, cy = poly.centroid.x, poly.centroid.y
    loc = nube(800, cx, cy, 0.006).to_crs(CRS_M)
    par = nube(150, cx, cy, 0.01).to_crs(CRS_M)
    sin = nube(1500, cx + 0.005, cy, 0.008)
    sin["hora"] = rng.integers(0, 24, len(sin))
    sin = sin[(sin.hora >= HORA_INI) & (sin.hora <= HORA_FIN)].to_crs(CRS_M)
    return G, poly, loc, par, sin


def main():
    global N_PARES, N_DIAS
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    if a.demo:
        N_PARES = 60
        N_DIAS = 5
        G, poly, loc, par, sin = demo()
        EJEMPLO["o"] = (-70.669, -33.459); EJEMPLO["z"] = (-70.64, -33.435)
    else:
        G, poly, loc, par = cargar_grafo()
        sin = cargar_siniestros(poly)
    print(f"Grafo: {G.number_of_nodes()} nodos, {G.number_of_edges()} arcos")
    t = indice_riesgo(G, sin, loc, par)
    marcar_grafo(G, t)
    t.drop(columns="geometry").to_csv(os.path.join(OUT, "tramos_riesgo.csv"), index=False)
    print("Evaluando pares origen-destino...")
    df = evaluar(G)
    df.to_csv(os.path.join(OUT, "pares.csv"), index=False)
    res = resumir(df)
    res.to_csv(os.path.join(OUT, "sensibilidad.csv"), index=False)
    print(res.round(2).to_string(index=False))
    print("Simulando jornadas de reparto...")
    jor = jornada(G)
    jor.to_csv(os.path.join(OUT, "jornadas.csv"), index=False)
    rob = robustez(t)
    rob.to_csv(os.path.join(OUT, "robustez.csv"), index=False)
    escribir_tex_extra(jor, rob)
    print(rob.round(1).to_string(index=False))
    ejs = ejemplos(G, df)
    escribir_tex(res, t, df, ejs)
    figuras(G, t, ejs)
    print(f"Listo en {time.time() - t0:.0f} s. Archivos en ./{OUT}/")


if __name__ == "__main__":
    main()
