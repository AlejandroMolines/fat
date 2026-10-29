# Backend y frontend

Consulta el [README de la carpeta principal](../README.md) para instalación y uso. Ejecuta el servidor desde la carpeta principal con `python .\fomo-app\server.py`.

El backend usa solo la biblioteca estándar de Python. `server.py` sirve la API local y los ficheros de `web/`; `engine.py` gestiona la cartera virtual; `fomo_integration.py` consulta FOMOAPI y procesa las alertas. El archivo `data/fomo.env` contiene la credencial API configurada y debe mantenerse privado.
