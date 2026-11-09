# Ruteo de mínimo tiempo con restricción de riesgo para repartidores de última milla

Prototipo del proyecto semestral (Facultad de Ingeniería y Ciencias, Universidad Diego Portales, 2026).

**Autores:** Dante Libiot, Matías Vásquez, Luciano Bosio y Agustín Zúñiga.

## Qué hace

Para un repartidor que debe ir de una entrega a la siguiente dentro de la comuna de Santiago, el programa busca la **ruta de menor tiempo que no pase por tramos críticos**. Un tramo es crítico si está en el 1 % más riesgoso de la comuna en siniestros de tránsito (CONASET) o en riesgo delictual estimado.

```
min  Σ t_e · x_e
s.a. conservación de flujo entre origen o y destino z
     x_e = 0  para todo tramo crítico (salvo el primero y el último)
     x_e ∈ {0, 1}
```

Como la restricción de riesgo solo elimina arcos, el problema se resuelve de forma exacta con el algoritmo de Dijkstra sobre la red sin tramos críticos.

## Datos

| Fuente | Uso |
|---|---|
| OpenStreetMap (vía OSMnx) | Red vial, velocidades, locales comerciales y paraderos |
| CONASET, Base SINIESTROS 2020–2025 | Siniestros de tránsito entre las 8 y las 21 horas |
| Literatura (Summers y Johnson 2017; Bernasco y Block 2011; Díaz et al. 2026) | Coeficientes del índice delictual |

Los datos se descargan automáticamente al ejecutar el programa.

## Cómo ejecutarlo

Requiere Python 3.10 o superior y conexión a internet.

```bash
pip install -r requirements.txt
python prototipo_rutas.py
```

La primera ejecución tarda unos minutos. Las descargas de OpenStreetMap se guardan en `cache/`, así que las siguientes son más rápidas. Si el servidor de OpenStreetMap está saturado, el programa reintenta solo con otros servidores.

Para probar el código sin internet, con una grilla sintética:

```bash
python prototipo_rutas.py --demo
```

Todos los parámetros (umbral `Q`, jornada, radios, coeficientes, número de pares y de jornadas) están al inicio de `prototipo_rutas.py`. Las semillas aleatorias están fijas, de modo que los resultados son reproducibles.

## Resultados

La carpeta `resultados/` contiene los resultados usados en el informe:

| Archivo | Contenido |
|---|---|
| `mapa_criticos.png` | Tramos críticos de la comuna |
| `mapa_ejemplo.png`, `mapa_ejemplo2.png` (y `.html`) | Ruta rápida y ruta segura en los dos ejemplos |
| `sensibilidad.csv` | Efecto del umbral `q` (1 %, 2,5 %, 5 %, 10 %) |
| `jornadas.csv` | Simulación de 30 jornadas de 25 entregas |
| `robustez.csv` | Estabilidad de los tramos críticos al variar los coeficientes |
| `tramos_riesgo.csv` | Índice de riesgo de cada tramo |
| `pares.csv` | Ruta rápida y segura para cada par origen-destino evaluado |
| `cifras.tex`, `cifras_extra.tex`, `tabla_*.tex` | Cifras y tablas que lee el informe en LaTeX |
