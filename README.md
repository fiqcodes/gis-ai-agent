# 🛰️ Satellite Analysis AI Agent

A LangGraph-powered satellite analysis agent using Google Earth Engine + Ollama (local LLM).

## Architecture

```
START → ROUTER
  ├── surface vars  → SURFACE_WORKER ──┐
  ├── atmo vars     → ATMO_WORKER ─────┤→ MERGE → CONTEXT → INSIGHT → EVALUATOR → END
  ├── lulc          → LULC_WORKER ─────┘
  ├── question      → QA → END
  └── unknown       → UNKNOWN → END
```

## File Structure

```
gis-ai-agent/
├── notebooks/
│   └── gis_agent.ipynb      # Original Jupyter notebook
├── agent.py                 # Main LangGraph agent + chat loop
├── gis_functions.py         # All GEE analysis functions
├── config.py                # GEE project + Ollama config
├── requirements.txt         # Python dependencies
└── README.md
```

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Authenticate Google Earth Engine
```bash
earthengine authenticate
```

### 3. Start Ollama
```bash
ollama serve
ollama pull gemma3:4b
```

### 4. Configure
Edit `config.py`:
```python
GEE_PROJECT  = 'your-gee-project-id'
OLLAMA_MODEL = 'gemma3:4b'   # or llama3.2, mistral, etc.
```

### 5. Run
```bash
python agent.py
```

## Available Variables

### Surface (Landsat 8)
| Variable | Description |
|----------|-------------|
| `ndvi` | Normalized Difference Vegetation Index |
| `evi` | Enhanced Vegetation Index |
| `savi` | Soil-Adjusted Vegetation Index |
| `ndwi` | Normalized Difference Water Index |
| `mndwi` | Modified NDWI |
| `ndbi` | Normalized Difference Built-up Index |
| `ui` | Urban Index |
| `nbi` | New Built-up Index |
| `bsi` | Bare Soil Index |
| `ndsi` | Normalized Difference Snow Index |
| `lst` | Land Surface Temperature |
| `uhi` | Urban Heat Island |
| `rgb` | True Color (RGB) |

### Atmospheric (Sentinel-5P)
| Variable | Description |
|----------|-------------|
| `co` | Carbon Monoxide |
| `ch4` | Methane |
| `no2` | Nitrogen Dioxide |
| `so2` | Sulfur Dioxide |
| `aerosol` | Absorbing Aerosol Index |
| `o3` | Ozone |
| `gpp` | Gross Primary Production |
| `burned` | Burned Area |
| `ffpi` | Fossil Fuel Pollution Index |

### Land Cover
| Variable | Description |
|----------|-------------|
| `lulc` | Land Cover Classification (Random Forest + ESA WorldCover) |

## Example Prompts

```
Show NDVI in Jakarta from 2023-01-01 to 2023-12-31
Analyze NO2 and CO pollution in Beijing in 2022
LST and UHI in Cairo, summer 2021
Land cover in Bali from 2022-01-01 to 2022-12-31
Land cover in Hokkaido from 2022-04-01 to 2022-10-31
What does FFPI measure?
```

## LULC Method

Land cover classification uses:
- **Labels (Y):** ESA WorldCover 2021 (10m, public GEE dataset)
- **Features (X):** Landsat 8 bands + spectral indices (12 features)
- **Algorithm:** Random Forest (200 trees)
- **Training scale:** 100m (aligns ESA labels with Landsat features)
- **Prediction scale:** 30m (Landsat native resolution)
- **Classes:** Water, Trees, Flooded Veg, Crops, Built Area, Bare Ground, Snow/Ice, Rangeland

## Best Regions for LULC

Islands and provinces work best:
- **Bali, Indonesia** — 5 classes, tropical diversity
- **Hokkaido, Japan** — 5 classes, forest + agriculture
- **Punjab, Pakistan** — 5 classes, cropland dominant
- **Dubai, UAE** — 2 classes, desert + urban
- **Harris County, USA** — 5 classes, urban + forest
