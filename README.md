# Fomo Paper Trader

Aplicación local para seguir rankings y alertas de FOMOAPI y simular la copia de operaciones con **100 € virtuales**. Incluye frontend, servidor/API local, motor de cartera, integración con FOMOAPI y datos de ejecución.

## Iniciar en Windows

1. Instala Python 3.10 o posterior.
2. Abre esta carpeta y ejecuta `start.bat` (o abre PowerShell y ejecuta `python .\fomo-app\server.py`).
3. Abre [http://127.0.0.1:8765](http://127.0.0.1:8765).
4. Para detener el servidor, vuelve a su consola y pulsa `Ctrl+C`.

No requiere paquetes externos de Python. La clave configurada se encuentra en `fomo-app\data\fomo.env`; el servidor la lee localmente y no la expone al navegador. El archivo se incluye porque se solicitó el proyecto completo con su configuración API. Mantén la carpeta privada y no la subas a repositorios públicos ni la envíes a terceros.

## Qué incluye

- Dashboard responsive de tema oscuro, con gráfica SVG de patrimonio y filtros de 24 h, 7 d, 30 d o todo el historial.
- Ranking FOMOAPI configurable: 24 h (últimas 24 horas, ventana móvil), 7 d, 30 d o histórico. Se pueden combinar varios periodos; el score usa el promedio de los percentiles seleccionados.
- Selección del top 3, 5, 10, 15 o 20 traders y control para pausar o reanudar la copia virtual.
- Feed de alertas FOMOAPI, deduplicación de eventos y estado del proveedor.
- Simulación local de compras y ventas con capital de 100 €, comisión, slippage y retraso máximo.
- Cartera abierta, P&L, historial de señales, operaciones cerradas y persistencia en JSON.
- Formulario de señales manuales y una demo ficticia para comprobar la pantalla.

La clasificación de 24 h representa las últimas 24 horas que ofrece el proveedor, no el día natural de Madrid. El proveedor puede limitar o retrasar las señales. Solo se copia a la cartera virtual: esta aplicación no conecta una wallet, no firma transacciones ni envía órdenes reales.

Por defecto se combinan 24 h y 7 d; el ranking se actualiza automáticamente cada seis horas y se puede forzar una actualización con el botón. FOMOAPI mide estas consultas por créditos: el panel muestra el saldo restante que devuelve el proveedor. Revisa ese saldo antes de activar más ventanas o pedir actualizaciones manuales. La documentación del proveedor explica los costes por endpoint y que los sondeos de alertas sin novedades no consumen créditos ([API Reference](https://fomoapi.io/docs#credits--headers)).

## Estructura

```text
FomoPaperTrader/
├── README.md
├── start.bat
└── fomo-app/
    ├── server.py              # Servidor HTTP y API local
    ├── engine.py              # Motor de simulación y cartera
    ├── fomo_integration.py    # Cliente FOMOAPI y alertas
    ├── web/                   # Interfaz, estilos y gráfica
    └── data/                  # Clave API y estado local
```

## API local

- `GET /api/health` — estado del servidor y conexión.
- `GET /api/state` — cuenta, cartera, señales, ranking y gráfica.
- `POST /api/fomo/refresh` — actualizar el ranking.
- `POST /api/fomo/settings` — elegir ventanas de ranking y número de traders.
- `POST /api/fomo/toggle` — pausar o reanudar la copia simulada.
- `POST /api/signals` — registrar una señal manual.
- `POST /api/quotes` — actualizar una cotización manual.
- `POST /api/close` — cerrar una posición virtual.
- `POST /api/demo` — cargar la operación ficticia de ejemplo.
- `POST /api/reset` — reiniciar la cuenta y el historial a 100 €.

Los cambios de ranking se guardan en `fomo-app\data\fomo_settings.json`. Cuenta, operaciones, cotizaciones y cursor del feed quedan en `fomo-app\data\`.
