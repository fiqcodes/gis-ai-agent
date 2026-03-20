# GIS Agent WebApp

Split-panel geospatial AI agent — chat interface + interactive Leaflet map.

## Folder Structure

```
gis-ai-agent/
├── agent.py
├── gis_functions.py
├── config.py
├── requirements.txt
└── webapp/
    ├── app.py                    ← Flask backend
    ├── requirements_webapp.txt
    ├── templates/
    │   └── index.html
    └── static/
        ├── css/style.css
        └── js/app.js
```

## Setup

```bash
pip install flask flask-cors
```

## Run

```bash
# From the gis-ai-agent root directory:
cd webapp
python app.py
```

Open: http://localhost:5000

## Features

- **Chat interface** — type natural language queries
- **Plan widget** — shows agent steps with progress
- **Interactive map** — Leaflet + ESRI satellite basemap
- **Draw ROI** — polygon or rectangle, name it, reference with @name
- **Real-time analysis** — connects to GEE + Ollama agent
- **Image overlays** — saved JPGs overlaid on map with correct bbox
- **Plotly charts** — monthly trends, LULC pie + bar, inline in chat
- **Layer manager** — toggle, zoom, remove layers

## Example Prompts

```
Show NDVI in Bali from 2022-01-01 to 2022-12-31
Analyze NO2 pollution in Beijing 2022
Land cover in Hokkaido from 2022-04-01 to 2022-10-31
LST and UHI in Jakarta 2023
```
