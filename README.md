# NotiYa – Financial News & Sentiment Analysis Platform

[![Python](https://img.shields.io/badge/Python-3.12%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Vercel](https://img.shields.io/badge/Deployed_on-Vercel-black?logo=vercel&logoColor=white)](https://vercel.com/)
[![NLP](https://img.shields.io/badge/NLP-VADER%20%2B%20Custom%20Finance%20Lexicon-green)](https://github.com/cjhutto/vaderSentiment)
[![APIs](https://img.shields.io/badge/Data_Sources-Finnhub%20%7C%20Tiingo%20%7C%20CoinGecko-orange)](https://finnhub.io/)

Plataforma analítica y API Serverless desarrollada en Python para la ingesta, traducción y análisis de sentimiento en tiempo real sobre noticias bursátiles y de criptomonedas.

---

## Descripción

NotiYa es una aplicación diseñada para que inversores y analistas evalúen rápidamente el pulso del mercado. El sistema consume feeds de noticias financieras internacionales en tiempo real, normaliza y traduce los titulares mediante un pipeline con preservación de entidades clave (tickers y marcas), y calcula métricas cuantitativas de sentimiento (Bullish, Bearish, Neutral) ajustadas con ponderaciones específicas para finanzas.

Toda la plataforma —tanto los endpoints de la API como la interfaz visual reactiva— está empaquetada como un microservicio sin frameworks web externos, utilizando exclusivamente la biblioteca estándar HTTP de Python optimizada para Vercel Serverless Functions.

---

## Arquitectura y Características Principales

### 1. Motor NLP y Ajuste Léxico Financiero
- **Sentiment Analysis Personalizado:** Utiliza `vaderSentiment` extendido con un diccionario de refuerzo para jerga financiera (`surge`, `rally`, `plunge`, `slumps`, `record high`, etc.), clasificando los titulares en rangos de convicción (Bullish, Bearish o Neutral).
- **Pipeline de Traducción Protegida:** Módulo que enmascara mediante expresiones regulares tickers bursátiles (ej: `AAPL`, `NVDA`, `BTC`) y términos protegidos (`FED`, `SEC`, `WALL STREET`) antes de invocar a `deep-translator`, restaurándolos posteriormente para evitar alteraciones léxicas.

### 2. Ingesta Multicanal de Datos de Mercado
- **Finnhub API:** Ingesta de noticias corporativas de los últimos 7 días y titulares generales macroeconómicos.
- **Tiingo API:** Batch request unificado para alimentar la cinta de cotizaciones (Ticker Tape) con índices y activos clave (`SPX`, `DJI`, `NDX`, `GLD`, `WTI`, `AAPL`, `NVDA`).
- **CoinGecko API:** Detección de tendencias cripto y cotizaciones en vivo para activos descentralizados.
- **Yahoo Finance & Fallback Engine:** Algoritmo de desduplicación y scoring de relevancia (`merge_articles`) según coincidencia contextual de tickers.

### 3. Estrategia de Caching y Resiliencia (Anti-F5 Shield)
- Memoria caché en tiempo de ejecución con ventanas TTL (Time-To-Live de 5 a 10 minutos) para cotizaciones, noticias generales y traducciones.
- Evita el agotamiento de cuotas en APIs de terceros (rate limiting) y garantiza respuestas sub-segundo en peticiones concurrentes.

### 4. Dashboard Web Integrado (SPA)
- Frontend responsivo nativo embebido con soporte para modo claro/oscuro persistente (`localStorage`).
- **Cinta de precios continua:** Ticker animado por aceleración de hardware (`translateZ(0)`).
- **Watchlist interactiva:** Panel de favoritos con reordenamiento mediante Drag & Drop nativo y barras de sentimiento consolidado por activo.
- **Filtros rápidos:** Filtrado en tiempo real de noticias por polaridad (Solo Bullish / Solo Bearish).

---

## Tecnologías Utilizadas

- **Lenguaje:** Python 3.12+
- **Procesamiento de Lenguaje Natural:** `vaderSentiment`, `deep-translator`
- **Networking & Server:** `http.server`, `urllib.request`, `json`, `re`
- **APIs Financieras:** Finnhub, Tiingo, CoinGecko
- **Infraestructura & CI/CD:** Vercel Functions (Serverless Runtime)

---

## Especificación de la API

| Método | Endpoint | Parámetros | Descripción |
| :--- | :--- | :--- | :--- |
| `GET` | `/` | `?ticker={SYMBOL}` | Retorna el dashboard web interactivo. |
| `GET` | `/api/news` | `ticker` (ej: `NVDA`) | Devuelve artículos analizados, score de sentimiento y metadata. |
| `GET` | `/api/top-news` | — | Titulares macroeconómicos globales traducidos. |
| `GET` | `/api/ticker-tape` | — | Datos de precios consolidados en lote para índices y commodities. |

---

## Estructura del Repositorio

```text
├── api/
│   └── index.py         # Entry point: Serverless handler, scraping, NLP y UI
├── requirements.txt     # Dependencias del entorno
├── vercel.json          # Reglas de enrutamiento y runtime
├── .gitignore           # Archivos temporales y cachés excluidos
└── README.md            # Documentación técnica
```

---

## Instalación y Ejecución Local

1. **Clonar el repositorio:**
   ```bash
   git clone [https://github.com/MaxiPetrini/Trabajo-Practico-Topicos-de-Economia-Digital.git](https://github.com/MaxiPetrini/Trabajo-Practico-Topicos-de-Economia-Digital.git)
   cd Trabajo-Practico-Topicos-de-Economia-Digital
   ```

2. **Crear y activar un entorno virtual:**
   ```bash
   python -m venv venv
   # En Windows:
   venv\Scripts\activate
   # En Linux/macOS:
   source venv/bin/activate
   ```

3. **Instalar dependencias:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Ejecutar el servidor:**
   ```bash
   python api/index.py
   ```
   *Acceso local desde el navegador en `http://127.0.0.1:8000`.*

---

## Demo en Vivo

La aplicación se encuentra desplegada y operativa en:  
**[NotiYa en Vercel](https://notiya.vercel.app)**
