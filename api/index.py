from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import random
import re
import socketserver
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from deep_translator import GoogleTranslator
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer

# Finnhub API key
FINNHUB_KEY = os.getenv("FINNHUB_KEY", "")

# Tiingo API Key (EXCLUSIVO PARA LA CINTA DE PRECIOS EN LOTE)
TIINGO_KEY = os.getenv("TIINGO_KEY", "")

def fetch_finnhub_news(symbol: str) -> list[dict]:
  """
  Consulta Finnhub para obtener noticias de la empresa (últimos 7 días).
  LIMITADO A 5 ARTÍCULOS MÁXIMO para evitar timeouts.
  Mapea los campos a los usados por merge_articles.
  """
  if not FINNHUB_KEY:
    print(f"⚠️ FINNHUB_KEY no configurado")
    return []
  
  today = datetime.now(timezone.utc).date()
  week_ago = today - timedelta(days=7)
  url = (
    f"https://finnhub.io/api/v1/company-news?symbol={urllib.parse.quote(symbol)}"
    f"&from={week_ago.strftime('%Y-%m-%d')}&to={today.strftime('%Y-%m-%d')}&token={FINNHUB_KEY}"
  )
  print(f"    🔍 Finnhub GET {url[:80]}...")
  try:
    request = urllib.request.Request(url, headers=get_headers())
    # TIMEOUT AGRESIVO: 3 segundos máximo
    with urllib.request.urlopen(request, timeout=3) as response:
      data = json.load(response)
    print(f"    ✓ Finnhub devolvió {len(data)} artículos crudos")
    
    articles = []
    for item in data[:20]:
      if not item.get("headline") or not item.get("url"):
        continue
      
      published_time = item.get("datetime", 0)
      if isinstance(published_time, str):
        try:
          published_time = int(published_time)
        except (ValueError, TypeError):
          published_time = 0
      else:
        published_time = int(published_time) if published_time else 0
      
      articles.append({
        "title": item["headline"],
        "link": item["url"],
        "providerPublishTime": published_time,
        "publisher": item.get("source", "Finnhub"),
        "description": item.get("summary", ""),
        "relatedTickers": [symbol],
      })
    
    print(f"    ✓ Finnhub: {len(articles)} artículos procesados")
    return articles
    
  except Exception as e:
    print(f"    ⊘ Finnhub error ({type(e).__name__}): {str(e)[:60]}")
    return []

def fetch_general_market_news() -> list[dict]:
  """Consulta noticias generales de Finnhub para el panel lateral."""
  global GLOBAL_NEWS_CACHE
  now = time.time()
  cached_time, cached_data = GLOBAL_NEWS_CACHE

  # Si pasaron menos de 5 minutos, devolver caché
  if cached_data and (now - cached_time < 300):
    print("⚡ [ANTI-F5] Devolviendo Noticias Globales desde la memoria")
    return cached_data

  if not FINNHUB_KEY:
    return []
  url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
  try:
    request = urllib.request.Request(url, headers=get_headers())
    with urllib.request.urlopen(request, timeout=5) as response:  # REDUCIDO A 5 SEG
      data = json.load(response)
    articles = []
    for item in data[:8]: # Traemos 8 titulares
      articles.append({
        "title": translate_title(item["headline"]),
        "link": item["url"],
        "published": int(item.get("datetime", 0)),
        "publisher": item.get("source", "Finnhub")
      })
            
    # Guardamos en la memoria global
    GLOBAL_NEWS_CACHE = (now, articles)
    return articles
  except Exception as e:
    print(f"⚠️ Error al consultar noticias generales: {type(e).__name__}: {e}", file=__import__('sys').stderr)
    return []
USER_AGENTS = [
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
  "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
]

def get_headers():
  return {"User-Agent": random.choice(USER_AGENTS)}

# Sentiment analysis

# Inicializar el analizador una sola vez
sentiment_analyzer = SentimentIntensityAnalyzer()

def analyze_sentiment(text: str) -> str:
  """
  Analiza el sentimiento de un texto (típicamente un titular financiero).
  Devuelve 'Bullish' si es positivo, 'Bearish' si es negativo, o 'Neutral'.
  """
  # Palabras financieras que mejoran la sensibilidad
  finance_boost = {
    'surge': 2.0, 'rally': 2.0, 'record high': 2.0, 'soars': 2.0, 'jumps': 1.5, 'beats': 1.5,
    'plunge': -2.0, 'crash': -2.0, 'plummets': -2.0, 'tumbles': -1.5, 'misses': -1.5, 'slumps': -1.5
  }
  text_lower = text.lower()
  score = sentiment_analyzer.polarity_scores(text)
  compound = score['compound']
  # Ajuste por palabras clave financieras
  for word, boost in finance_boost.items():
    if word in text_lower:
      compound += boost * 0.1  # Ajuste leve
  if compound >= 0.15:
    return 'Bullish'
  elif compound <= -0.15:
    return 'Bearish'
  else:
    return 'Neutral'

# Palabras financieras y empresas que NO queremos traducir
PROTECTED_WORDS = [
    "APPLE", "GOLD", "META", "AMAZON", "TESLA", "ALPHABET", "NVIDIA", 
    "MICROSOFT", "BITCOIN", "ETHEREUM", "WALL STREET", "FED", "CEO", "SEC"
]

# NUEVO: Diccionario que funciona como memoria caché
TRANSLATION_CACHE: dict[str, str] = {}

def translate_title(text: str) -> str:
  if not text:
    return ""

  # 1. El escudo de la Caché: Si ya lo tradujimos antes, lo devolvemos al instante
  if text in TRANSLATION_CACHE:
    return TRANSLATION_CACHE[text]

  placeholders = {}
  counter = 0
  working_text = text

  # 2. Proteger las palabras de la lista
  for word in PROTECTED_WORDS:
    pattern = re.compile(rf'\b{word}\b', re.IGNORECASE)
    for match in pattern.finditer(working_text):
      original = match.group()
      token = f" TKN{counter}TKN "
      placeholders[token.strip()] = original
      working_text = working_text.replace(original, token, 1)
      counter += 1

  # 3. Proteger Tickers (Cualquier palabra de 2 a 5 letras TODA EN MAYÚSCULAS)
  ticker_pattern = re.compile(r'\b[A-Z]{2,5}\b')
  for match in ticker_pattern.finditer(working_text):
    original = match.group()
    if original not in placeholders.values():
      token = f" TKN{counter}TKN "
      placeholders[token.strip()] = original
      working_text = working_text.replace(original, token, 1)
      counter += 1

  # 4. Mandar a traducir el texto enmascarado
  try:
    translator = GoogleTranslator(source='en', target='es')
    translated = translator.translate(working_text)
  except Exception as e:
    print(f"⚠️ Error en traducción: {e}", file=__import__('sys').stderr)
    return text

  # 5. Desenmascarar (Volver a poner las palabras originales)
  for token, original_word in placeholders.items():
    token_pattern = re.compile(rf'\b{token}\b', re.IGNORECASE)
    translated = token_pattern.sub(original_word, translated)

  # 6. Arreglar la mayúscula inicial SIN arruinar las internas (Corrección visual)
  if translated:
    final_translation = translated[0].upper() + translated[1:]
  else:
    final_translation = translated

  # 7. NUEVO: Guardar el resultado final en la caché para el futuro
  TRANSLATION_CACHE[text] = final_translation

  return final_translation

HOST = "127.0.0.1"
PORT = 8000
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
COINGECKO_TRENDS_URL = "https://api.coingecko.com/api/v3/search/trending"
COINGECKO_GLOBAL_URL = "https://api.coingecko.com/api/v3/global"
COINGECKO_SEARCH_URL = "https://api.coingecko.com/api/v3/search"
COINGECKO_MARKETS_URL = "https://api.coingecko.com/api/v3/coins/markets"
NEWSAPI_URL = "https://newsapi.org/v2/everything"
CRYPTOPANIC_URL = "https://cryptopanic.com/api/v1/posts/"
NEWSAPI_KEY = ""  # Set your NewsAPI key here if needed
CRYPTOPANIC_KEY = ""  # Set your CryptoPanic key here if needed
CRYPTO_CODES = {
  "BTC",
  "ETH",
  "SOL",
  "XRP",
  "DOGE",
  "ADA",
  "BNB",
  "TRX",
  "USDT",
  "USDC",
  "LTC",
  "BCH",
  "DOT",
  "AVAX",
  "LINK",
  "XLM",
  "TON",
  "HBAR",
  "SUI",
  "APT",
  "UNI",
  "NEAR",
  "AAVE",
  "ETC",
  "XMR",
  "ALGO",
  "ICP",
  "INJ",
  "ATOM",
  "FIL",
  "OP",
  "ARB",
  "MATIC",
  "POL",
  "SHIB",
  "PEPE",
}
TICKER_INFO_CACHE: dict[str, dict] = {}
COINGECKO_ID_CACHE: dict[str, str] = {}

# NUEVOS DICCIONARIOS DE CACHÉ TEMPORAL PARA YAHOO
YAHOO_SEARCH_CACHE: dict[str, tuple[float, dict]] = {}
QUOTE_CACHE: dict[str, tuple[float, dict]] = {}

# CAJA FUERTE GLOBAL PARA LA CINTA DE PRECIOS
GLOBAL_TICKER_TAPE_CACHE: tuple[float, list] = (0, [])

# --- NUEVO: SISTEMA ANTI-F5 PARA NOTICIAS ---
NEWS_CACHE: dict[str, tuple[float, dict]] = {}
GLOBAL_NEWS_CACHE: tuple[float, list] = (0, [])


def fetch_yahoo_news_from_symbol(symbol: str) -> list[dict]:
  """Obtiene noticias de Yahoo usando la API de búsqueda de Yahoo Finance."""
  try:
    payload = fetch_yahoo_payload(symbol)
    news_list = payload.get("news", [])
    articles = []
    for item in news_list:
      if not item.get("title") or not item.get("link"):
        continue
      articles.append({
        "title": item.get("title", ""),
        "link": item.get("link", ""),
        "publisher": item.get("source", "Yahoo Finance"),
        "providerPublishTime": int(item.get("providerPublishTime", 0)) if item.get("providerPublishTime") else 0,
        "description": item.get("summary", ""),
        "relatedTickers": [symbol],
      })
    return articles
  except Exception as e:
    print(f"⚠️ Error en fetch_yahoo_news_from_symbol: {e}")
    pass
  return []


def fetch_yahoo_payload(query_text: str) -> dict:
  now = time.time()
  # Si buscamos esta noticia hace menos de 10 minutos (600 seg), la sacamos de la memoria
  if query_text in YAHOO_SEARCH_CACHE:
    cached_time, cached_data = YAHOO_SEARCH_CACHE[query_text]
    if now - cached_time < 600:
      return cached_data

  params = urllib.parse.urlencode({"q": query_text, "quotesCount": 1, "newsCount": 20})
  url = f"{YAHOO_SEARCH_URL}?{params}"
  request = urllib.request.Request(url, headers=get_headers())

  with urllib.request.urlopen(request, timeout=10) as response:  # REDUCIDO A 10 SEG
    data = json.load(response)
    YAHOO_SEARCH_CACHE[query_text] = (now, data) # Guardamos en memoria
    return data


def normalize_text(value: str) -> str:
    return " ".join(value.strip().upper().split())


def build_company_label(payload: dict, fallback_symbol: str) -> str:
    quotes = payload.get("quotes", [])
    if not quotes:
        return fallback_symbol

    quote = quotes[0]
    return quote.get("shortname") or quote.get("longname") or fallback_symbol


def build_search_terms(symbol: str) -> list[str]:
    terms = [symbol]
    # Si es un ticker conocido, agregamos el nombre oficial
    special_cases = {
      "NVDA": "NVIDIA Corporation",
      "AAPL": "Apple Inc",
      "GGAL": "Grupo Financiero Galicia",
      "TSLA": "Tesla Inc"
    }
    if symbol in special_cases:
      terms.append(special_cases[symbol])
    return terms


def extract_crypto_code(symbol: str) -> str:
  return symbol.split("-")[0].strip().upper()


def get_ticker_info(symbol: str) -> dict:
  cached = TICKER_INFO_CACHE.get(symbol)
  if cached:
    return cached

  try:
    # IMPORTANTE: Aquí NO llamamos a quote_symbol_for_asset para evitar circular dependency
    # Solo usamos el símbolo directamente
    quote_symbol = symbol
    
    # Traducción simple de índices para Yahoo sin circular dependency
    mapping = {
      'SPX': '^GSPC', 'DJIA': '^DJI', 'CCMP': '^IXIC', 'IXIC': '^IXIC',
      'VIX': '^VIX', 'GOLD': 'GLD', 'WTIUSD': 'CL=F', 'EUR': 'EUR=X', 'GBP': 'GBP=X'
    }
    quote_symbol = mapping.get(quote_symbol.upper(), quote_symbol)
    
    url = f"https://query1.finance.yahoo.com/v7/finance/quote?symbols={urllib.parse.quote(quote_symbol)}"
    request = urllib.request.Request(url, headers=get_headers())
    with urllib.request.urlopen(request, timeout=5) as response:  # Reduced timeout
      payload = json.load(response)

    quotes = payload.get("quoteResponse", {}).get("result", [])
    info = quotes[0] if quotes else {}
  except Exception as e:
    print(f"    ⊘ get_ticker_info error for {symbol}: {type(e).__name__}")
    info = {}

  # La clave de la barra superior: Solo guarda si Yahoo devolvió info real
  if info:
    TICKER_INFO_CACHE[symbol] = info
  return info


def quote_symbol_for_asset(symbol: str) -> str:
  if is_crypto_asset(symbol):
    return f"{extract_crypto_code(symbol)}-USD"

  # Traductor automático de índices y materias primas para Yahoo
  mapping = {
    'SPX': '^GSPC',
    'DJIA': '^DJI',
    'CCMP': '^IXIC',
    'IXIC': '^IXIC',
    'VIX': '^VIX',
    'GOLD': 'GLD',
    'WTIUSD': 'CL=F',
    'EUR': 'EUR=X',
    'GBP': 'GBP=X'
  }
  return mapping.get(symbol.upper(), symbol)


def resolve_coingecko_id(symbol: str) -> str | None:
  base_symbol = extract_crypto_code(symbol)
  cached = COINGECKO_ID_CACHE.get(base_symbol)
  if cached:
    return cached

  try:
    request = urllib.request.Request(
      f"{COINGECKO_SEARCH_URL}?{urllib.parse.urlencode({'query': base_symbol})}",
      headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
      payload = json.load(response)
  except Exception:
    return None

  coins = payload.get("coins", [])
  for coin in coins:
    if str(coin.get("symbol", "")).upper() == base_symbol:
      coin_id = coin.get("id")
      if coin_id:
        COINGECKO_ID_CACHE[base_symbol] = coin_id
        return coin_id

  for coin in coins:
    coin_id = coin.get("id")
    if coin_id:
      COINGECKO_ID_CACHE[base_symbol] = coin_id
      return coin_id

  return None


def fetch_crypto_quote(symbol: str) -> dict:
  coin_id = resolve_coingecko_id(symbol)
  if not coin_id:
    return {"price": None, "change": None, "change_pct": None, "error": "No se pudo resolver el precio de la cripto."}

  try:
    params = urllib.parse.urlencode({"vs_currency": "usd", "ids": coin_id, "price_change_percentage": "24h"})
    request = urllib.request.Request(
      f"{COINGECKO_MARKETS_URL}?{params}",
      headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
      payload = json.load(response)
  except Exception as exc:
    return {"price": None, "change": None, "change_pct": None, "error": f"Error al obtener cotización cripto: {exc}"}

  market = payload[0] if payload else {}
  current_price = market.get("current_price")
  change = market.get("price_change_24h")
  change_pct_raw = market.get("price_change_percentage_24h")

  if current_price is None:
    return {"price": None, "change": None, "change_pct": None, "error": "No se pudo obtener la cotización cripto."}

  return {
    "price": current_price,
    "change": change,
    "change_pct": (change_pct_raw / 100) if change_pct_raw is not None else None,
    "direction": "subió" if (change or 0) >= 0 else "bajó",
    "error": "",
  }


def is_crypto_asset(symbol: str) -> bool:
  base_symbol = extract_crypto_code(symbol)
  # Primero chequear la lista de criptos conocidos
  if base_symbol in CRYPTO_CODES:
    return True
  
  # Si no está en la lista conocida, asumimos que NO es crypto
  # para evitar hacer llamadas HTTP costosas
  return False


def parse_published_timestamp(value: str | int | None) -> int:
  if not value:
    return 0

  if isinstance(value, int):
    return value

  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return 0

  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)

  return int(parsed.timestamp())


def fetch_coingecko_trending() -> dict:
    """Fetch trending cryptocurrencies and news from CoinGecko (free, no auth required)."""
    try:
        request = urllib.request.Request(COINGECKO_TRENDS_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)
    except Exception:
        return {}


def fetch_coingecko_articles(currency_code: str) -> list[dict]:
    """Fetch crypto news from CoinGecko's trending endpoint.
    
    Maps trending coins to article-like objects with title, link, publisher, and timestamp.
    """
    try:
        payload = fetch_coingecko_trending()
    except Exception:
        return []

    articles: list[dict] = []
    
    for category in ["coins", "exchanges", "nfts"]:
        items = payload.get(category, [])
        for item in items[:20]:  # Limit to 20 items per category
            coin_data = item.get("item", {})
            coin_id = coin_data.get("id", "")
            coin_symbol = coin_data.get("symbol", "").upper()
            coin_name = coin_data.get("name", "")
            coin_thumb = coin_data.get("thumb", "")
            
            # Match by symbol code
            if coin_symbol != currency_code:
                continue
            
            # Create article-like entry from trending coin data
            title = f"{coin_name} ({coin_symbol}) - Trending on CoinGecko"
            link = f"https://www.coingecko.com/en/coins/{coin_id}" if coin_id else ""
            
            if not link:
                continue
            
            # Use current time as published (CoinGecko trending doesn't have publish time)
            published = int(time.time())
            
            articles.append(
                {
                    "title": title,
                    "link": link,
                    "publisher": "CoinGecko Trending",
                    "providerPublishTime": published,
                    "relatedTickers": [currency_code],
                }
            )
    
    return articles


def fetch_finnhub_quote(symbol: str) -> dict:
  """Extrae el precio actual usando la API de Finnhub para evitar bloqueos de Yahoo"""
  if not FINNHUB_KEY:
    return {"error": "Sin API Key"}

  url = f"https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(symbol)}&token={FINNHUB_KEY}"
  try:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=10) as response:
      data = json.load(response)
      # Finnhub devuelve 'c' como precio actual y 'dp' como porcentaje
      if "c" in data and data["c"] != 0:
        return {
          "price": float(data.get("c", 0)),
          "change": float(data.get("d", 0)),
          "change_pct": float(data.get("dp", 0)), # Finnhub ya lo da en formato porcentaje
          "error": ""
        }
      return {"error": "Sin datos en Finnhub"}
  except Exception as e:
    return {"error": str(e)}


def fetch_quote(symbol: str) -> dict:
  """Función deshabilitada - no se muestran precios individuales."""
  return {"price": None, "change": None, "change_pct": None, "error": ""}


def is_within_last_week(timestamp: int) -> bool:
    if not timestamp:
        return False
    now = time.time()
    one_week_ago = now - (7 * 24 * 60 * 60)
    return timestamp >= one_week_ago


def article_contains_symbol(article: dict, symbol: str) -> bool:
  title = (article.get("title") or "").upper()
  description = (article.get("description") or "").upper()
  symbol_upper = symbol.upper()
  
  # Diccionario para buscar también por nombre de empresa
  company_names = {
      "NVDA": "NVIDIA",
      "AAPL": "APPLE",
      "GGAL": "GALICIA",
      "TSLA": "TESLA",
      "MSFT": "MICROSOFT",
      "AMZN": "AMAZON",
      "META": "META",
      "YPF": "YPF"
  }
  
  # Verificamos si está el ticker
  if symbol_upper in title or symbol_upper in description:
      return True
      
  # Verificamos si está el nombre de la empresa
  if symbol_upper in company_names:
      company = company_names[symbol_upper]
      if company in title or company in description:
          return True
          
  return False


def fetch_newsapi_articles(symbol: str) -> list[dict]:
  if not NEWSAPI_KEY:
    return []
  try:
    params = urllib.parse.urlencode({
      "q": symbol,
      "sortBy": "publishedAt",
      "language": "en",
      "pageSize": 20,
      "apiKey": NEWSAPI_KEY,
    })
    url = f"{NEWSAPI_URL}?{params}"
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
      data = json.load(response)
      articles = []
      for item in data.get("articles", []):
        if article_contains_symbol(item, symbol):
          pub_time = item.get("publishedAt", "")
          try:
            timestamp = int(datetime.fromisoformat(pub_time.replace("Z", "+00:00")).timestamp())
          except:
            timestamp = int(time.time())
          articles.append({
            "title": item.get("title", ""),
            "link": item.get("url", ""),
            "publisher": item.get("source", {}).get("name", "NewsAPI"),
            "providerPublishTime": timestamp,
            "description": item.get("description", ""),
            "relatedTickers": [symbol],
          })
      return articles
  except Exception:
    return []


def merge_articles(existing: list[dict], incoming: list[dict], symbol: str) -> list[dict]:
    """
    Combina artículos existentes con nuevos, evitando duplicados y limitando cantidad.
    """
    seen_links = {article["link"] for article in existing}
    articles = list(existing)
    
    # Separar Finnhub del resto
    finnhub_items = []
    other_items = []
    
    for item in incoming:
        publisher = str(item.get("publisher", "")).lower()
        if "finnhub" in publisher:
            finnhub_items.append(item)
        else:
            other_items.append(item)
    
    # Procesar Finnhub primero (máximo 20 para evitar timeouts)
    for item in finnhub_items[:20]:
        title = item.get("title", "").strip()
        link = item.get("link", "").strip()
        published = item.get("providerPublishTime") or item.get("published") or 0

        if not title or not link or link in seen_links:
            continue
        if not is_within_last_week(published):
            print(f"  ⊘ Artículo descartado - fuera de rango temporal (timestamp={published}, within_week={is_within_last_week(published)})")
            continue

        title_es = translate_title(title)
        sentiment = analyze_sentiment(title)
        related_tickers = item.get("relatedTickers", [symbol])
        
        articles.append({
          "title": title_es,
          "link": link,
          "publisher": item.get("publisher", "Finnhub"),
          "published": published,
          "score": 100,
          "sentiment": sentiment,
          "relatedTickers": related_tickers,
        })
        seen_links.add(link)
        print(f"  ✓ Artículo agregado: {title_es[:50]}...")

    # Procesar otras fuentes
    for item in other_items:
        title = item.get("title", "").strip()
        link = item.get("link", "").strip()
        published = item.get("providerPublishTime") or item.get("published") or 0

        if not title or not link or link in seen_links:
            continue
        if not is_within_last_week(published):
            continue

        has_text_match = article_contains_symbol(item, symbol)
        related_tickers = [t.strip().upper() for t in item.get("relatedTickers", []) if isinstance(t, str)]
        is_related = symbol in related_tickers
        
        if not has_text_match and not is_related:
            continue

        title_text = title.upper()
        score = 0
        if symbol in title_text:
            score += 3
        if symbol in related_tickers:
            score += 2

        sentiment = analyze_sentiment(title)
        title_es = translate_title(title)

        articles.append({
          "title": title_es,
          "link": link,
          "publisher": item.get("publisher", "Yahoo Finance"),
          "published": published,
          "score": score,
          "sentiment": sentiment,
          "relatedTickers": related_tickers,
        })
        seen_links.add(link)

    articles.sort(key=lambda article: (article["score"], article["published"] or 0), reverse=True)
    return articles


def fetch_news(ticker: str) -> dict:
  symbol = normalize_text(ticker)
  if not symbol:
    return {
      "ticker": "", "company": "", "articles": [],
      "quote": {"price": None, "change": None, "change_pct": None, "error": "Ingresa un ticker."},
      "error": "Ingresa un ticker para buscar noticias."
    }

  # --- SISTEMA ANTI-F5: Revisar si ya buscamos esto hace menos de 5 minutos ---
  now = time.time()
  if symbol in NEWS_CACHE:
    cached_time, cached_data = NEWS_CACHE[symbol]
    if now - cached_time < 300: # 300 segundos = 5 minutos
      print(f"⚡ [ANTI-F5] Devolviendo noticias de {symbol} desde la memoria al instante")
      return cached_data

  print(f"\n🔍 INICIANDO fetch_news para {symbol}")
  company = symbol
  crypto_asset = is_crypto_asset(symbol)
  crypto_symbol = extract_crypto_code(symbol) if crypto_asset else symbol
  articles: list[dict] = []

  # --- PASO 1: FINNHUB (Única fuente de noticias para acciones) ---
  print(f"  [1/3] Consultando Finnhub...")
  try:
    finnhub_articles = fetch_finnhub_news(symbol)
    if finnhub_articles:
      articles = merge_articles(articles, finnhub_articles, symbol)
      print(f"  ✓ Finnhub: {len(articles)} artículos procesados")
    else:
      print(f"  ⚠️ Finnhub no devolvió noticias para {symbol}")
  except Exception as e:
    print(f"  ⚠️ Error en Finnhub: {e}")

  # --- PASO 2: CoinGecko (Solo si es crypto) ---
  if crypto_asset and len(articles) < 5:
    print(f"  [2/3] Consultando CoinGecko (crypto)...")
    try:
      crypto_articles = fetch_coingecko_articles(crypto_symbol)
      if crypto_articles:
        articles = merge_articles(articles, crypto_articles, crypto_symbol)
        print(f"  ✓ CoinGecko: {len(articles)} artículos procesados")
    except Exception as e:
      print(f"  ⚠️ Error en CoinGecko: {e}")

  # Sin cotización individual (función deshabilitada)
  quote = {"price": None, "change": None, "change_pct": None, "error": ""}

  print(f"📊 RESULTADO FINAL: {len(articles)} artículos para {symbol}\n")

  result = {
    "ticker": symbol,
    "company": company,
    "articles": articles[:20],
    "quote": quote,
    "error": "",
  }

  # Guardamos el resultado en la caja fuerte por 5 minutos
  NEWS_CACHE[symbol] = (now, result)

  return result


class handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, BrokenPipeError):
            # Si el usuario recargó la página (F5) y cortó la conexión, lo ignoramos en silencio
            pass

    def _send_html(self, body: str) -> None:
        payload = body.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionAbortedError, BrokenPipeError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/news":
            params = urllib.parse.parse_qs(parsed.query)
            ticker = params.get("ticker", [""])[0]
            self._send_json(fetch_news(ticker))
            return

        if parsed.path == "/api/top-news":
          self._send_json({"articles": fetch_general_market_news()})
          return

        if parsed.path == "/api/quote":
          self._send_json({"error": "Endpoint deshabilitado"})
          return

        if parsed.path == "/api/ticker-tape":
          global GLOBAL_TICKER_TAPE_CACHE
          now = time.time()
          last_time, last_data = GLOBAL_TICKER_TAPE_CACHE

          # --- CAJA FUERTE (F5 PROTEGIDO - 5 MINUTOS) ---
          if last_data and (now - last_time < 300):
            self._send_json({'data': last_data})
            return

          results = []

          # 1. BITCOIN sigue por CoinGecko
          try:
            btc_quote = fetch_crypto_quote("BTC")
            if not btc_quote.get("error"):
              results.append({
                'symbol': 'BTC',
                'price': btc_quote.get('price', 0),
                'change': btc_quote.get('change', 0),
                'changePercent': btc_quote.get('change_pct', 0) * 100,
              })
          except Exception: pass

          # 2. EL RESTO DE LA CINTA USA TIINGO (Batch Request Unificado)
          tiingo_map = {
            'SPY': 'SPX',   # S&P 500
            'DIA': 'DJI',   # Dow Jones
            'QQQ': 'NDX',   # Nasdaq 100
            'GLD': 'GLD',   # Oro
            'WTI': 'WTI',   # Petróleo WTI
            'AAPL': 'AAPL',
            'NVDA': 'NVDA'
          }
            
          try:
            symbols_str = ",".join(tiingo_map.keys())
            url = f"https://api.tiingo.com/iex/?tickers={symbols_str}&token={TIINGO_KEY}"
                
            request = urllib.request.Request(url, headers=get_headers())
            with urllib.request.urlopen(request, timeout=10) as response:
              data = json.load(response)
                    
            for item in data:
              t_sym = item.get("ticker", "").upper()
              if t_sym in tiingo_map:
                # Extraemos el cierre de ayer de forma segura
                raw_prev = item.get("prevClose")
                prev_close = float(raw_prev if raw_prev is not None else 0)
                        
                # Usamos 'tngoLast' (consolidado total de Wall Street) con fallback a 'last'
                raw_tngo = item.get("tngoLast")
                raw_last = item.get("last")
                        
                if raw_tngo is not None:
                  price = float(raw_tngo)
                elif raw_last is not None:
                  price = float(raw_last)
                else:
                  price = prev_close
                            
                # Calculamos la variación real
                change = 0
                change_pct = 0
                if price and prev_close:
                  change = price - prev_close
                  change_pct = (change / prev_close) * 100
                            
                results.append({
                  'symbol': tiingo_map[t_sym],
                  'price': price,
                  'change': change,
                  'changePercent': change_pct
                })
                
            if len(results) > 2:
              GLOBAL_TICKER_TAPE_CACHE = (now, results)

          except Exception as e:
            print(f"⚠️ Error en Tiingo Ticker Tape: {e}", file=__import__('sys').stderr)
            if last_data:
              self._send_json({'data': last_data})
              return

          self._send_json({'data': results})
          return

        if parsed.path not in {"/", "/index.html"}:
            self.send_error(404, "Not found")
            return

        initial_ticker = normalize_text(urllib.parse.parse_qs(parsed.query).get("ticker", [""])[0])
        html = """<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>NotiYa – Noticias por ticker</title>
  <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&display=swap" rel="stylesheet">
    <style>
    /* Sentiment badges */
    .sentiment-bullish {
      display: inline-block;
      background: #16a34a;
      color: #fff;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 700;
      padding: 3px 13px 3px 11px;
      margin-bottom: 4px;
      margin-right: 7px;
      letter-spacing: 0.01em;
      vertical-align: middle;
    }
    .sentiment-bearish {
      display: inline-block;
      background: #dc2626;
      color: #fff;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 700;
      padding: 3px 13px 3px 11px;
      margin-bottom: 4px;
      margin-right: 7px;
      letter-spacing: 0.01em;
      vertical-align: middle;
    }
    .sentiment-neutral {
      display: inline-block;
      background: #a3a3a3;
      color: #fff;
      border-radius: 999px;
      font-size: 0.82rem;
      font-weight: 700;
      padding: 3px 13px 3px 11px;
      margin-bottom: 4px;
      margin-right: 7px;
      letter-spacing: 0.01em;
      vertical-align: middle;
    }
    :root {
      color-scheme: light;
      --bg: #f5f3ee;
      --bg-accent: rgba(15, 118, 110, 0.10);
      --bg-accent-2: rgba(180, 83, 9, 0.08);
      --panel: rgba(255, 255, 255, 0.78);
      --panel-strong: #ffffff;
      --text: #171717;
      --muted: #66615c;
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --border: rgba(23, 23, 23, 0.09);
      --shadow: 0 24px 80px rgba(15, 23, 42, 0.10);
      --chart-line: rgba(15, 118, 110, 0.16);
      --grid-line: rgba(23, 23, 23, 0.04);
      --ticker-height: 48px;
    }

    body[data-theme="dark"] {
      color-scheme: dark;
      --bg: #0f1720;
      --bg-accent: rgba(34, 197, 94, 0.10);
      --bg-accent-2: rgba(245, 158, 11, 0.08);
      --panel: rgba(17, 24, 39, 0.82);
      --panel-strong: rgba(15, 23, 42, 0.96);
      --text: #f4f7fb;
      --muted: #a9b4c2;
      --accent: #7dd3fc;
      --accent-soft: rgba(125, 211, 252, 0.12);
      --border: rgba(148, 163, 184, 0.16);
      --shadow: 0 24px 80px rgba(0, 0, 0, 0.34);
      --chart-line: rgba(125, 211, 252, 0.18);
      --grid-line: rgba(148, 163, 184, 0.09);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      font-family: 'IBM Plex Sans', sans-serif;
      color: var(--text);
      background-color: var(--bg);
      position: relative;
    }

    /* 1. El resplandor superior (ahora fijo para que no desaparezca al bajar) */
    body::before {
      content: "";
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      height: 100vh;
      background: radial-gradient(circle at 50% -10%, var(--bg-accent), transparent 60%);
      z-index: -2;
      pointer-events: none;
    }

    /* 2. NUEVO: Cuadrícula financiera (estilo gráfico de análisis técnico) */
    body::after {
      content: "";
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      height: 100vh;
      /* Usamos la variable --grid-line que ya tenés creada en tu tema */
      background-image: 
        linear-gradient(var(--grid-line) 1px, transparent 1px),
        linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
      background-size: 36px 36px;
      background-position: center top;
      z-index: -1;
      pointer-events: none;
      /* Difuminamos la grilla hacia el centro para mantener la lectura limpia */
      -webkit-mask-image: linear-gradient(to bottom, rgba(0,0,0,1) 0%, rgba(0,0,0,0) 75%);
      mask-image: linear-gradient(to bottom, rgba(0,0,0,1) 0%, rgba(0,0,0,0) 75%);
    }

    .shell {
      max-width: 1200px;
      margin: 0 auto;
      padding: calc(40px + var(--ticker-height)) 18px 56px;
    }

    /* Contenedor principal de dos columnas */
    .dashboard-layout {
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 24px;
      align-items: start;
    }

    /* Ajuste para que en celulares se vea una sola columna */
    @media (max-width: 950px) {
      .dashboard-layout {
        grid-template-columns: 1fr;
      }
    }

    .hero {
      display: grid;
      gap: 14px;
      grid-template-columns: 1fr;
      justify-items: center;
      text-align: center;
      margin-bottom: 22px;
    }

    .kicker {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      width: fit-content;
      padding: 9px 13px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.88rem;
      font-weight: 700;
      letter-spacing: 0.02em;
    }

    h1 {
      margin: 12px 0 0;
      font-size: clamp(2.3rem, 5vw, 4.6rem);
      line-height: 0.96;
      letter-spacing: -0.05em;
    }

    .hero-actions {
      display: none;
    }

    .theme-toggle {
      position: fixed;
      bottom: 24px; 
      left: 24px;   
      top: auto;
      right: auto;
      z-index: 100; /* Lo mandamos bien al fondo */
      /* ... el resto del código queda igual ... */
      border: 2px solid var(--border);
      border-radius: 50%;
      width: 44px;
      height: 44px;
      padding: 0;
      background: var(--panel);
      color: var(--text);
      font-size: 1.4rem;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1); /* Le agregamos una sombra sutil */
      transition: transform 0.12s ease, background-color 0.12s ease, border-color 0.12s ease;
    }

    .theme-toggle:hover {
      transform: scale(1.08);
      border-color: rgba(15, 118, 110, 0.24);
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 26px;
      box-shadow: var(--shadow);
      backdrop-filter: none;
      position: relative;
    }

    .searchbar {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      padding: 20px;
      border-bottom: 1px solid var(--border);
    }

    .field label {
      display: block;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 700;
    }

    .field input {
      width: 100%;
      height: 56px;
      padding: 0 18px;
      border-radius: 16px;
      border: 1px solid var(--border);
      outline: none;
      background: var(--panel-strong);
      color: var(--text);
      font-size: 1.1rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      transition: border-color 0.18s ease, box-shadow 0.18s ease, transform 0.18s ease;
    }

    .field input:focus {
      border-color: rgba(15, 118, 110, 0.45);
      box-shadow: 0 0 0 4px rgba(15, 118, 110, 0.12);
    }

    .button {
      align-self: end;
      height: 56px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 0;
      border-radius: 16px;
      padding: 0 24px;
      background: linear-gradient(135deg, var(--accent), #0f4e4b);
      color: white;
      font-size: 0.98rem;
      font-weight: 700;
      cursor: pointer;
      box-shadow: 0 14px 26px rgba(15, 118, 110, 0.20);
      transition: transform 0.12s ease, box-shadow 0.12s ease;
    }

    .button:hover {
      transform: translateY(-1px);
      box-shadow: 0 18px 30px rgba(15, 118, 110, 0.25);
    }

    /* Ajustes del botón EXCLUSIVOS para el Modo Oscuro */
    body[data-theme="dark"] .button {
      background: linear-gradient(135deg, #0284c7, #0369a1); /* Azul profesional para contrastar de noche */
      color: #ffffff;
      /* Sombra negra profunda para que resalte sobre el fondo oscuro */
      box-shadow: 0 14px 26px rgba(0, 0, 0, 0.6);
    }

    body[data-theme="dark"] .button:hover {
      /* Sombra más grande + un leve resplandor azul al pasar el mouse */
      box-shadow: 0 18px 30px rgba(0, 0, 0, 0.8), 0 0 15px rgba(2, 132, 199, 0.25);
    }

    .field {
      position: relative;
    }

    .ticker-menu {
      display: none;
      position: absolute;
      top: 100%;
      left: 0;
      right: 0;
      margin-top: 4px;
      background: var(--panel-strong);
      border: 1px solid var(--border);
      border-radius: 12px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2); 
      z-index: 1000;
      max-height: 300px;
      overflow-y: auto;
      
      /* Esta es la línea mágica que aísla el scroll del menú del resto de la página */
      overscroll-behavior: contain; 
      
      font-family: 'IBM Plex Sans', sans-serif;
    }

    .ticker-menu.show {
      display: block;
    }

    .menu-section {
      padding: 0;
    }

    .menu-section:not(:last-child) {
      border-bottom: 1px solid var(--border);
    }

    .section-title {
      padding: 10px 14px 6px;
      font-size: 0.8rem;
      font-weight: 700;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }

    .menu-item {
      padding: 12px 16px;
      cursor: pointer;
      color: var(--text);
      font-size: 0.95rem;
      font-weight: 600; /* Letra un poco más gruesa para darle peso a los tickers */
      letter-spacing: 0.02em;
      transition: background-color 0.1s ease, padding-left 0.15s ease; /* Pequeño efecto de deslizamiento al pasar el mouse */
      user-select: none;
    }

    .menu-item:hover {
      background-color: var(--accent-soft);
      color: var(--accent);
      padding-left: 20px; /* Al pasar el mouse, el texto se mueve levemente a la derecha */
    }

    .content {
      padding: 16px 20px 20px;
    }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 10px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 11px;
      border-radius: 999px;
      background: var(--panel-strong);
      border: 1px solid var(--border);
      color: var(--text);
      font-size: 0.92rem;
      font-weight: 700;
    }

    .status {
      display: none;
      margin-bottom: 14px;
      padding: 14px 16px;
      border-radius: 16px;
      font-size: 0.95rem;
      line-height: 1.5;
    }

    .status.show { display: block; }
    .status.info {
      display: block;
      background: rgba(15, 118, 110, 0.08);
      border: 1px solid rgba(15, 118, 110, 0.16);
      color: #115e59;
    }
    .status.error {
      display: block;
      background: rgba(220, 38, 38, 0.08);
      border: 1px solid rgba(220, 38, 38, 0.16);
      color: #991b1b;
    }

    .loading {
      display: none;
      margin-bottom: 14px;
      color: var(--muted);
      font-weight: 700;
    }

    .loading.show { display: block; }

    .list {
      display: grid;
      gap: 12px;
    }

    .card {
      display: grid;
      gap: 8px;
      padding: 16px 16px 14px;
      border-radius: 18px;
      background: var(--panel-strong);
      border: 1px solid var(--border);
      transition: transform 0.12s ease, border-color 0.12s ease;
      contain: content;
      content-visibility: auto;
      contain-intrinsic-size: 120px;
      transform: translateZ(0);
    }

    .card:hover {
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.22);
    }

    .card h3 {
      margin: 0;
      font-size: 1.03rem;
      line-height: 1.38;
    }

    .card h3 a {
      color: inherit;
      text-decoration: none;
    }

    .card h3 a:hover { color: var(--accent); }

    .small {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-size: 0.92rem;
    }

    .quote-section {
      background: linear-gradient(135deg, var(--accent-soft), rgba(180, 83, 9, 0.08));
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 18px 16px;
      margin-bottom: 18px;
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 20px;
      align-items: center;
      content-visibility: auto;
      contain-intrinsic-size: 110px;
      transform: translateZ(0);
    }

    .quote-price {
      display: grid;
      gap: 4px;
    }

    .price-label {
      color: var(--muted);
      font-size: 0.88rem;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.03em;
    }

    .price-value {
      font-size: 2rem;
      font-weight: 700;
      color: var(--text);
    }

    .price-change {
      display: flex;
      align-items: center;
      gap: 8px;
      font-size: 1rem;
      font-weight: 700;
    }

    .price-change.up { color: #16a34a; }
    .price-change.down { color: #dc2626; }

    .ticker-tape {
      background: var(--panel-strong);
      border-bottom: 1px solid var(--border);
      /* remove vertical padding and use flex centering to perfectly center contents */
      padding: 0;
      overflow: hidden;
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      height: var(--ticker-height);
      display: flex;
      align-items: center;
      z-index: 9999;
      box-shadow: none;
    }

    body.is-scrolling .card,
    body.is-scrolling .quote-section,
    body.is-scrolling .empty {
      transition: none;
    }

    /* Ticker keeps scrolling even when user scrolls — only pauses on hover */
    body.is-scrolling .ticker-tape-container {
      animation-play-state: running;
    }

    /* Pausar la cinta solo en escritorio. En móvil el "toque" lo pausaba para siempre. */
    @media (hover: hover) and (pointer: fine) {
      .ticker-tape:hover .ticker-tape-container {
        animation-play-state: paused;
      }
    }

    .ticker-item {
      flex-shrink: 0;
      display: flex;
      align-items: center;
      gap: 14px; /* Separación interna perfecta (símbolo - precio - cambio) */
      font-size: 0.95rem;
      white-space: nowrap;
      position: relative;
    }

    /* El clásico puntito separador de los tickers financieros */
    .ticker-item::after {
      content: "";
      position: absolute;
      right: -22px; /* Se posiciona exactamente en el medio del gap */
      top: 50%;
      transform: translateY(-50%);
      width: 4px;
      height: 4px;
      border-radius: 50%;
      background-color: var(--muted);
      opacity: 0.5;
    }

    .ticker-symbol {
      color: var(--muted);
      font-weight: 800;
      letter-spacing: 0.04em;
      /* Eliminamos los min-width que rompían la alineación */
    }

    .ticker-price {
      color: var(--text);
      font-weight: 700;
    }

    .ticker-change {
      font-weight: 700;
    }

    .ticker-change.up {
      color: #16a34a;
    }

    .ticker-change.down {
      color: #dc2626;
    }

    @keyframes scroll-left {
      0% { transform: translateX(0); }
      100% { transform: translateX(-50%); }
    }

    .ticker-tape-container {
      display: flex;
      gap: 40px;
      width: max-content;
      min-width: max-content;
      white-space: nowrap;
      flex-wrap: nowrap;
      will-change: transform;
      transform: translateZ(0); /* Fuerza el uso de la placa de video */
      backface-visibility: hidden;
      animation: scroll-left 30s linear infinite;
    }

    @media (max-width: 780px) {
      .ticker-item {
        font-size: 0.85rem;
        gap: 10px;
      }
      .ticker-item::after {
        right: -22px;
      }
      /* Eliminamos los min-width de mobile */
    }

    @media (max-width: 780px) {
      .hero { grid-template-columns: 1fr; }
      .searchbar { grid-template-columns: 1fr; }
      .button { width: 100%; }
      .quote-section { grid-template-columns: 1fr; gap: 12px; }
    }

    /* --- PANEL LATERAL DESLIZABLE (DRAWER) --- */
    /* --- PANEL LATERAL DESLIZABLE IZQUIERDO (DRAWER) --- */
    .left-drawer {
      position: fixed;
      top: var(--ticker-height);
      left: 0;
      bottom: 0;
      width: 360px;
      background: var(--panel-strong);
      border-right: 1px solid var(--border);
      box-shadow: 8px 0 30px rgba(0, 0, 0, 0.15);
      z-index: 1000;
      transform: translateX(-100%);
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
    }

    /* Los paneles solo se abren al pasar el mouse en computadoras.
       En celulares dependerán exclusivamente del toque (JavaScript) */
    @media (hover: hover) and (pointer: fine) {
      .left-drawer:hover {
        transform: translateX(0);
      }
      .favorites-panel:hover {
        transform: translateX(0);
      }
    }

    .drawer-handle {
      position: absolute;
      right: -42px;
      top: 40px;
      width: 42px;
      height: 220px;
      background: var(--panel);
      border: 1px solid var(--border);
      border-left: none;
      border-radius: 0 12px 12px 0;
      box-shadow: 4px 4px 12px rgba(0, 0, 0, 0.05);
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      writing-mode: vertical-rl;
      text-orientation: mixed;
      transform: none;
      font-weight: 900;
      color: var(--text);
      letter-spacing: 0.15em;
      font-size: 0.9rem;
      text-transform: uppercase;
      text-align: center;
      transition: background-color 0.2s ease, color 0.2s ease;
    }

    .drawer-handle:hover {
      background: var(--bg-accent);
      color: var(--accent);
    }

    .drawer-content {
      width: 100%;
      height: 100%;
      padding: 24px;
      overflow-y: auto;
      overscroll-behavior: contain;
    }

    /* --- PANEL DE FAVORITOS (DERECHA FIJA) --- */
    .favorites-panel {
      position: fixed;
      top: var(--ticker-height);
      right: 0;
      bottom: 0;
      width: 260px;
      background: var(--panel-strong);
      border-left: 1px solid var(--border);
      box-shadow: -8px 0 30px rgba(0, 0, 0, 0.10);
      z-index: 998;
      display: flex;
      flex-direction: column;
      transform: translateX(100%);
      transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
    }

    .favorites-handle {
      position: absolute;
      left: -42px;
      top: 40px;
      width: 42px;
      height: 220px;
      background: var(--panel);
      border: 1px solid var(--border);

      /* Invertimos los bordes y sombras porque vamos a rotar todo 180 grados */
      border-left: none; 
      border-radius: 0 12px 12px 0; 
      box-shadow: 4px -4px 12px rgba(0, 0, 0, 0.05); 

      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      writing-mode: vertical-rl;
      text-orientation: mixed;

      /* ¡Este es el giro mágico que da vuelta el texto! */
      transform: rotate(180deg); 

      font-weight: 900;
      color: var(--text);
      letter-spacing: 0.15em;
      font-size: 0.9rem;
      text-transform: uppercase;
      text-align: center;
      transition: background-color 0.2s ease, color 0.2s ease;
    }

    .favorites-handle:hover {
      background: var(--bg-accent);
      color: var(--accent);
    }

    .favorites-content {
      flex: 1;
      padding: 20px 16px;
      overflow-y: auto;
      overscroll-behavior: contain;
    }

    .favorites-title {
      margin: 0 0 16px 0;
      font-size: 1rem;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      color: var(--text);
      border-bottom: 1px solid var(--border);
      padding-bottom: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .fav-card {
      display: flex;
      flex-direction: column;
      gap: 4px;
      padding: 12px 14px;
      border-radius: 14px;
      background: var(--panel);
      border: 1px solid var(--border);
      margin-bottom: 10px;
      cursor: pointer;
      transition: transform 0.12s ease, border-color 0.12s ease;
      position: relative;
    }

    .fav-card:hover {
      transform: translateY(-1px);
      border-color: rgba(15, 118, 110, 0.22);
    }

    /* Estilos para Drag & Drop en Favoritos */
    .fav-card {
      cursor: grab;
    }
    .fav-card:active {
      cursor: grabbing;
    }
    .fav-card.dragging {
      opacity: 0.5;
      transform: scale(0.98);
      border: 1px dashed var(--muted);
    }

    .fav-ticker-name {
      font-size: 0.98rem;
      font-weight: 800;
      letter-spacing: 0.04em;
      color: var(--text);
    }

    .fav-price {
      font-size: 1.1rem;
      font-weight: 700;
      color: var(--text);
    }

    .fav-change {
      font-size: 0.85rem;
      font-weight: 700;
    }

    .fav-change.up { color: #16a34a; }
    .fav-change.down { color: #dc2626; }

    .fav-remove {
      position: absolute;
      top: 8px;
      right: 10px;
      background: none;
      border: none;
      cursor: pointer;
      color: var(--muted);
      font-size: 1rem;
      padding: 2px;
      line-height: 1;
      opacity: 0;
      transition: opacity 0.15s ease, color 0.15s ease;
    }

    .fav-card:hover .fav-remove {
      opacity: 1;
    }

    .fav-remove:hover {
      color: #dc2626;
    }

    .fav-loading {
      font-size: 0.82rem;
      color: var(--muted);
      margin-top: 2px;
    }

    .fav-empty {
      color: var(--muted);
      font-size: 0.9rem;
      text-align: center;
      padding: 24px 0;
      line-height: 1.6;
    }

    /* Botón estrella que aparece en el resultado de búsqueda */
    .star-btn {
      background: none;
      border: none;
      cursor: pointer;
      font-size: 1.3rem;
      line-height: 1;
      padding: 0 4px;
      color: var(--muted);
      transition: color 0.15s ease, transform 0.12s ease;
      vertical-align: middle;
      flex-shrink: 0;
    }

    .star-btn:hover {
      transform: scale(1.2);
      color: #f59e0b;
    }

    .star-btn.active {
      color: #f59e0b;
    }

    /* Fila de ticker con estrella */
    .ticker-header-row {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-bottom: 10px;
    }

    #asset-label {
      flex: 1;
    }

    @media (max-width: 950px) {
      /* Panel Izquierdo */
      .left-drawer {
        width: 85%;
        max-width: 320px;
      }
      .left-drawer.open {
        transform: translateX(0);
      }
      
      /* Panel de Favoritos (Lateral en móvil) */
      .favorites-panel {
        width: 85%;
        max-width: 320px;
        top: var(--ticker-height);
        bottom: 0;
        right: 0;
        left: auto;
        border-left: 1px solid var(--border);
        height: auto;
        transform: translateX(100%);
      }
      .favorites-panel.open {
        transform: translateX(0);
      }
      .favorites-handle {
        left: -42px;
        right: auto;
        top: 40px;
        width: 42px;
        height: 220px;
        border-left: none;
        border-radius: 0 12px 12px 0;
        writing-mode: vertical-rl;
        transform: rotate(180deg);
        box-shadow: 4px -4px 12px rgba(0, 0, 0, 0.05);
      }
      .favorites-content {
        margin-top: 0;
        height: 100%;
      }
    }

    /* Ocultar precios individuales y de favoritos forzosamente */
    .quote-section,
    .fav-price,
    .fav-change {
      display: none !important;
    }

    /* --- NUEVO: Estilos para Filtros de Noticias --- */
    .filters {
      display: flex;
      gap: 10px;
      margin-bottom: 16px;
      flex-wrap: wrap;
    }
    .filter-btn {
      background: var(--panel);
      border: 1px solid var(--border);
      color: var(--muted);
      padding: 6px 14px;
      border-radius: 999px;
      font-size: 0.85rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.2s ease;
    }
    .filter-btn:hover {
      background: var(--bg-accent);
      color: var(--text);
    }
    /* 1. Activo para "Relevantes" (Contraste elegante) */
    .filter-btn[data-filter="all"].active {
      background: var(--text);
      color: var(--bg);
      border-color: var(--text);
    }

    /* 2. Activo para "Solo Bullish" (Verde) */
    .filter-btn[data-filter="Bullish"].active {
      background: #16a34a;
      color: #ffffff;
      border-color: #16a34a;
    }

    /* 3. Activo para "Solo Bearish" (Rojo) */
    .filter-btn[data-filter="Bearish"].active {
      background: #dc2626;
      color: #ffffff;
      border-color: #dc2626;
    }
  </style>
</head>
<body>
  <section class="ticker-tape">
    <div class="ticker-tape-container" id="ticker-container">
      <div class="ticker-item">
        <span>Cargando datos del mercado...</span>
      </div>
    </div>
  </section>
  
  <div class="left-drawer">
    <div class="drawer-handle">
      Noticias Globales
    </div>
    <div class="drawer-content">
      <h2 style="margin-top:0; font-size:1.15rem; text-transform:uppercase; color:var(--text); border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:16px;">
        Wall Street Hoy
      </h2>
      <div class="list" id="general-news-results" style="gap:16px;">
        <div class="loading show">Cargando actualidad...</div>
      </div>
    </div>
  </div>

  <div class="favorites-panel" id="favorites-panel">
    <div class="favorites-handle">Favoritos</div>
    <div class="favorites-content">
      <h2 class="favorites-title">Mis Favoritos</h2>
      <div id="favorites-list">
        <div class="fav-empty">Buscá un ticker y tocá la ⭐ para agregarlo acá.</div>
      </div>
    </div>
  </div>

  <main class="shell">
    <section class="hero">
      <div>
        <div class="kicker">Noticias al instante</div>
        <h1>NotiYa – Noticias para invertir con claridad.</h1>
        <button class="theme-toggle" id="theme-toggle" type="button" aria-pressed="false" aria-label="Cambiar tema">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>
        </button>
      </div>
    </section>

    <section class="panel">
      <form class="searchbar" id="search-form">
        <div class="field">
          <label for="ticker">Ticker</label>
          <input id="ticker" name="ticker" value="__INITIAL_TICKER__" autocomplete="off" spellcheck="false" maxlength="12" />
          <div class="ticker-menu" id="ticker-menu"></div>
        </div>
        <button class="button" type="submit">Buscar noticias</button>
      </form>

      <div class="content">
        <div class="meta ticker-header-row">
          <div class="pill" id="asset-label">Activo: __INITIAL_TICKER__</div>
          <button class="star-btn" id="star-btn" title="Agregar a favoritos" aria-label="Agregar a favoritos">☆</button>
          <div class="pill" id="result-count">0 titulares</div>
        </div>
        <div id="quote-container"></div>
        <div class="status" id="message" aria-live="polite"></div>
        <div class="loading" id="loading">Consultando...</div>
        
        <div class="filters" id="news-filters" style="display: none;">
          <button class="filter-btn active" data-filter="all" type="button">Relevantes</button>
          <button class="filter-btn" data-filter="Bullish" type="button">🟢 Solo Bullish</button>
          <button class="filter-btn" data-filter="Bearish" type="button">🔴 Solo Bearish</button>
        </div>

        <div class="list" id="results"></div>
      </div>
    </section>
  </main>

  <script>
    const form = document.getElementById('search-form');
    const tickerInput = document.getElementById('ticker');
    const results = document.getElementById('results');
    const loading = document.getElementById('loading');
    const message = document.getElementById('message');
    const resultCount = document.getElementById('result-count');
    const assetLabel = document.getElementById('asset-label');
    const quoteContainer = document.getElementById('quote-container');
    const tickerMenu = document.getElementById('ticker-menu');
    const themeToggle = document.getElementById('theme-toggle');
    const TRENDING_TICKERS = ['BTC', 'AAPL', 'META', 'NVDA', 'TSLA'];
    const MAX_HISTORY = 3;
    const HISTORY_KEY = 'notiYa_search_history';
    const THEME_KEY = 'notiYa_theme';

    // Ticker Tape Data - Fetch from API
    async function renderTickerTape() {
      const container = document.getElementById('ticker-container');
      console.log('📡 Iniciando renderTickerTape...');
      try {
        const response = await fetch('/api/ticker-tape', { cache: 'no-store' });
        const result = await response.json();
        const tickerData = result.data || [];
        console.log(`📦 Datos recibidos: ${tickerData.length} activos`);
        if (tickerData.length === 0) {
          console.warn('⚠️ Ticker tape: sin datos disponibles');
          return;
        }
        const tickerMarkup = tickerData.map(item => {
          const isPositive = item.change >= 0;
          const changeClass = isPositive ? 'up' : 'down';
          const changeSymbol = isPositive ? '+' : '';
          return `
            <div class="ticker-item">
              <span class="ticker-symbol">${item.symbol}</span>
              <span class="ticker-price">$${item.price.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2})}</span>
              <span class="ticker-change ${changeClass}">${changeSymbol}${item.change.toFixed(2)} (${changeSymbol}${item.changePercent.toFixed(2)}%)</span>
            </div>
          `;
        }).join('');
        // Duplicamos el markup para que cuando termine la primera mitad, 
        // la segunda ya esté entrando y no se vea el hueco.
        container.innerHTML = tickerMarkup + tickerMarkup;
        
        // Limpiamos cualquier estilo de carga anterior
        container.style.background = 'transparent';
        container.style.minWidth = 'auto';
        console.log(`✅ Ticker actualizado: ${tickerData.length} activos duplicados`);
      } catch (error) {
        console.error('❌ Error fetching ticker data:', error);
      }
    }

    // Cargar ticker inicial y recargar cada 5 minutos (300.000 milisegundos) para evitar bloqueos
    renderTickerTape();
    setInterval(renderTickerTape, 300000);

    let scrollPauseTimer = null;
    window.addEventListener('scroll', () => {
      document.body.classList.add('is-scrolling');
      window.clearTimeout(scrollPauseTimer);
      scrollPauseTimer = window.setTimeout(() => {
        document.body.classList.remove('is-scrolling');
      }, 140);
    }, { passive: true });

    function setTheme(theme) {
      const nextTheme = theme === 'dark' ? 'dark' : 'light';
      document.body.dataset.theme = nextTheme;
      if (themeToggle) {
        const isDark = nextTheme === 'dark';
        themeToggle.setAttribute('aria-pressed', String(isDark));
        themeToggle.setAttribute('aria-label', isDark ? 'Cambiar a tema claro' : 'Cambiar a tema oscuro');

        // Iconos vectoriales de calidad premium
        const sunSVG = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>`;
        const moonSVG = `<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>`;

        // Cambiamos el contenido HTML interno por el SVG correspondiente
        themeToggle.innerHTML = isDark ? sunSVG : moonSVG;
      }
      localStorage.setItem(THEME_KEY, nextTheme);
    }

    const savedTheme = localStorage.getItem(THEME_KEY);
    setTheme(savedTheme === 'dark' ? 'dark' : 'light');

    themeToggle.addEventListener('click', () => {
      const nextTheme = document.body.dataset.theme === 'dark' ? 'light' : 'dark';
      setTheme(nextTheme);
    });

    function getSearchHistory() {
      try {
        const stored = localStorage.getItem(HISTORY_KEY);
        return stored ? JSON.parse(stored) : [];
      } catch {
        return [];
      }
    }

    function saveSearchHistory(ticker) {
      try {
        const history = getSearchHistory();
        const cleaned = ticker.trim().toUpperCase();
        const updated = [cleaned, ...history.filter(t => t !== cleaned)].slice(0, MAX_HISTORY);
        localStorage.setItem(HISTORY_KEY, JSON.stringify(updated));
      } catch {
        // localStorage no disponible, ignorar
      }
    }

    function renderTickerMenu() {
      const history = getSearchHistory();
      let html = '';

      if (history.length > 0) {
        html += '<div class="menu-section">';
        html += '<div class="section-title">Recientes</div>';
        history.forEach(ticker => {
          html += `<div class="menu-item" data-ticker="${ticker}">${ticker}</div>`;
        });
        html += '</div>';
      }

      html += '<div class="menu-section">';
      html += '<div class="section-title">Trending</div>';
      TRENDING_TICKERS.forEach(ticker => {
        html += `<div class="menu-item" data-ticker="${ticker}">${ticker}</div>`;
      });
      html += '</div>';

      tickerMenu.innerHTML = html;
      tickerMenu.classList.add('show');

      document.querySelectorAll('.menu-item').forEach(item => {
        item.addEventListener('click', (e) => {
          const selectedTicker = e.target.dataset.ticker;
          tickerInput.value = selectedTicker;
          tickerMenu.classList.remove('show');
          loadNews(selectedTicker);
        });
      });
    }

    tickerInput.addEventListener('focus', () => {
      renderTickerMenu();
    });

    document.addEventListener('click', (e) => {
      if (!e.target.closest('.field')) {
        tickerMenu.classList.remove('show');
      }
    });

    function formatDate(timestamp) {
      if (!timestamp) return 'Fecha no disponible';
      return new Intl.DateTimeFormat('es-AR', {
        dateStyle: 'medium',
        timeStyle: 'short'
      }).format(new Date(timestamp * 1000));
    }

    function formatPrice(price) {
      if (price === null || price === undefined) return 'N/A';
      return new Intl.NumberFormat('es-AR', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2
      }).format(price);
    }

    function renderQuote(quote) {
      // Función deshabilitada - no se muestran precios individuales
      quoteContainer.innerHTML = '';
    }

    function showMessage(text, kind) {
      message.textContent = text;
      message.className = 'status show ' + kind;
    }

    function clearMessage() {
      message.textContent = '';
      message.className = 'status';
    }

    let currentArticles = []; // Guardamos todas las noticias (hasta 20) en memoria

    // Función que dibuja exactamente 5 noticias dependiendo de lo que elijas
    function renderFilteredArticles(filterValue) {
      results.innerHTML = '';
      let filtered = [];

      // Filtramos la lista según el botón tocado
      if (filterValue === 'all') {
        filtered = currentArticles.slice(0, 5); // Las 5 generales más relevantes
      } else {
        // Buscamos todas las que coincidan con el sentimiento y cortamos en 5
        filtered = currentArticles.filter(a => a.sentiment === filterValue).slice(0, 5); 
      }

      // Actualizamos el contador de la derecha
      resultCount.textContent = `${filtered.length} titulares`;

      // Si no hay suficientes noticias para ese filtro
      if (filtered.length === 0) {
        results.innerHTML = `<div class="fav-empty" style="padding: 20px; text-align: center; color: var(--muted);">No hay suficientes noticias ${filterValue === 'Bullish' ? 'positivas' : 'negativas'} recientes para mostrar.</div>`;
        return;
      }

      // Dibujamos las tarjetas filtradas
      filtered.forEach((article) => {
        const card = document.createElement('article');
        card.className = 'card';
        card.dataset.sentiment = article.sentiment; 
        
        let sentimentClass = 'sentiment-neutral';
        let sentimentLabel = 'Neutral';
        if (article.sentiment === 'Bullish') {
          sentimentClass = 'sentiment-bullish';
          sentimentLabel = 'Bullish';
        } else if (article.sentiment === 'Bearish') {
          sentimentClass = 'sentiment-bearish';
          sentimentLabel = 'Bearish';
        }
        
        card.innerHTML = `
          <span class="${sentimentClass}">${sentimentLabel}</span>
          <h3><a href="${article.link}" target="_blank" rel="noreferrer noopener">${article.title}</a></h3>
          <div class="small">
            <span>${article.publisher || 'Yahoo Finance'}</span>
            <span>${formatDate(article.published)}</span>
          </div>
        `;
        results.appendChild(card);
      });
    }

    function render(data) {
      assetLabel.textContent = data.company ? `Activo: ${data.company} (${data.ticker})` : `Activo: ${data.ticker}`;
      
      renderQuote(data.quote);
      currentArticles = data.articles || [];

      const newsFilters = document.getElementById('news-filters');
      if (currentArticles.length > 0) {
        newsFilters.style.display = 'flex';
        // Resetear visualmente los botones
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        document.querySelector('.filter-btn[data-filter="all"]').classList.add('active');
      } else {
        newsFilters.style.display = 'none';
      }

      if (data.error) {
        showMessage(data.error, 'error');
        results.innerHTML = '';
        resultCount.textContent = '0 titulares';
      } else if (!currentArticles.length) {
        showMessage('No se encontraron noticias para ese ticker en la última semana.', 'info');
        results.innerHTML = '';
        resultCount.textContent = '0 titulares';
      } else {
        clearMessage();
        // Por defecto, al buscar, mostramos las 5 relevantes
        renderFilteredArticles('all'); 
      }
    }

    // --- Lógica de los botones de filtro ---
    document.querySelectorAll('.filter-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        // Cambiamos el botón activo
        document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        
        // Llamamos a la función que dibuja las 5 noticias de esa categoría
        renderFilteredArticles(btn.dataset.filter);
      });
    });

    async function loadGeneralNews() {
      const container = document.getElementById('general-news-results');
      try {
        const response = await fetch('/api/top-news');
        const data = await response.json();
        
        container.innerHTML = data.articles.map(article => `
          <article style="border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 4px;">
            <h4 style="margin:0 0 8px 0; font-size:0.95rem; line-height:1.4;">
              <a href="${article.link}" target="_blank" style="text-decoration:none; color:inherit;" onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='inherit'">${article.title}</a>
            </h4>
            <div class="small" style="font-size:0.8rem;">
              <span style="font-weight:bold; color:var(--text);">${article.publisher}</span> • <span>${formatDate(article.published)}</span>
            </div>
          </article>
        `).join('');
      } catch (e) {
        container.innerHTML = '<div class="status error">No se pudo cargar la actualidad.</div>';
      }
    }

    // Ejecutamos la carga inicial
    loadGeneralNews();

    async function loadNews(ticker) {
      const symbol = ticker.trim().toUpperCase();
      if (!symbol) {
        // En lugar de un error rojo, mostramos un mensaje azul de bienvenida
        showMessage('Buscá un ticker (ej: TSLA, META, BTC) para comenzar.', 'info');
        results.innerHTML = '';
        resultCount.textContent = '0 titulares';
        quoteContainer.innerHTML = '';
        assetLabel.textContent = 'Activo: -'; // Limpiamos la etiqueta
        return;
      }

      loading.classList.add('show');
      clearMessage();
      results.innerHTML = '';
      quoteContainer.innerHTML = '';
      resultCount.textContent = 'Buscando...';

      try {
        const response = await fetch(`/api/news?ticker=${encodeURIComponent(symbol)}`, { cache: 'no-store' });
        const data = await response.json();
        render(data);
        saveSearchHistory(symbol);
      } catch (error) {
        showMessage('No se pudo cargar la información. Reintentá en unos segundos.', 'error');
        resultCount.textContent = '0 titulares';
        quoteContainer.innerHTML = '';
      } finally {
        loading.classList.remove('show');
      }
    }

    form.addEventListener('submit', (event) => {
      event.preventDefault();
      loadNews(tickerInput.value);
    });

    loadGeneralNews();
    loadNews(tickerInput.value);

    // =============================================
    // SISTEMA DE FAVORITOS
    // =============================================
    const FAVORITES_KEY = 'notiYa_favorites';
    const favoritesPanel = document.getElementById('favorites-panel');
    const favoritesList = document.getElementById('favorites-list');
    const starBtn = document.getElementById('star-btn');

    // Ticker actualmente visible en pantalla
    let currentTicker = tickerInput.value.trim().toUpperCase() || '__INITIAL_TICKER__';

    function getFavorites() {
      try {
        const stored = localStorage.getItem(FAVORITES_KEY);
        return stored ? JSON.parse(stored) : [];
      } catch { return []; }
    }

    function saveFavorites(favs) {
      try {
        localStorage.setItem(FAVORITES_KEY, JSON.stringify(favs));
      } catch {}
    }

    function isFavorite(ticker) {
      return getFavorites().includes(ticker.toUpperCase());
    }

    function toggleFavorite(ticker) {
      const symbol = ticker.toUpperCase();
      let favs = getFavorites();
      if (favs.includes(symbol)) {
        favs = favs.filter(t => t !== symbol);
      } else {
        favs = [symbol, ...favs];
      }
      saveFavorites(favs);
      updateStarButton(symbol);
      renderFavoritesPanel();
    }

    function updateStarButton(ticker) {
      const fav = isFavorite(ticker);
      starBtn.textContent = fav ? '★' : '☆';
      starBtn.classList.toggle('active', fav);
      starBtn.title = fav ? 'Quitar de favoritos' : 'Agregar a favoritos';
    }

    starBtn.addEventListener('click', () => {
      if (currentTicker) toggleFavorite(currentTicker);
    });

    // Actualizar ticker visible cuando carga un nuevo resultado
    const _origRender = render;
    render = function(data) {
      _origRender(data);
      currentTicker = data.ticker ? data.ticker.toUpperCase() : currentTicker;
      updateStarButton(currentTicker);
    };

    async function fetchFavoriteQuote(ticker) {
      // Función deshabilitada - no se muestran precios individuales
      return null;
    }

    async function renderFavoritesPanel() {
      const favs = getFavorites();

      // Mostrar/ocultar el panel según haya favoritos
      if (favs.length > 0) {
        favoritesPanel.classList.add('has-favorites');
      } else {
        favoritesPanel.classList.remove('has-favorites');
      }

      if (favs.length === 0) {
        favoritesList.innerHTML = '<div class="fav-empty">Buscá un ticker y tocá la ⭐ para agregarlo acá.</div>';
        return;
      }

      // Renderizar la lista de favoritos sin precio, pero con espacio para sentimiento
      favoritesList.innerHTML = favs.map(ticker => `
        <div class="fav-card" id="fav-${ticker}" data-ticker="${ticker}" draggable="true">
          <div style="display:flex; align-items:center; justify-content:space-between;">
            <span class="fav-ticker-name">${ticker}</span>
            <button class="fav-remove" data-ticker="${ticker}" title="Quitar favorito">✕</button>
          </div>
          <div class="fav-sentiment" id="fav-sentiment-${ticker}">Cargando...</div>
        </div>
      `).join('');

      // Attach remove listeners
      document.querySelectorAll('.fav-remove').forEach(btn => {
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          const t = btn.dataset.ticker;
          const favs2 = getFavorites().filter(x => x !== t);
          saveFavorites(favs2);
          if (currentTicker === t) updateStarButton(t);
          renderFavoritesPanel();
        });
      });

      // Click en tarjeta => buscar ese ticker
      document.querySelectorAll('.fav-card').forEach(card => {
        card.addEventListener('click', (e) => {
          if (e.target.classList.contains('fav-remove')) return;
          const t = card.dataset.ticker;
          tickerInput.value = t;
          loadNews(t);
        });
      });

      // --- NUEVO: Lógica de Drag & Drop ---
      let draggedItem = null;
      
      document.querySelectorAll('.fav-card').forEach(card => {
        // Cuando empezamos a arrastrar
        card.addEventListener('dragstart', function(e) {
          draggedItem = this;
          setTimeout(() => this.classList.add('dragging'), 0);
          e.dataTransfer.effectAllowed = 'move';
        });

        // Cuando soltamos el click (termina el arrastre)
        card.addEventListener('dragend', function() {
          this.classList.remove('dragging');
          draggedItem = null;
          this.style.borderTop = "";
          this.style.borderBottom = "";
          
          // Leer el nuevo orden visual y guardarlo en el LocalStorage
          const newOrder = Array.from(favoritesList.querySelectorAll('.fav-card')).map(c => c.dataset.ticker);
          saveFavorites(newOrder);
        });

        // Mientras pasamos la tarjeta por encima de otras
        card.addEventListener('dragover', function(e) {
          e.preventDefault(); // Necesario para permitir soltar (drop)
          const bounding = this.getBoundingClientRect();
          const offset = bounding.y + (bounding.height / 2);
          
          // Dibuja una línea verde para indicar dónde va a caer
          if (e.clientY - offset > 0) {
            this.style.borderBottom = "2px solid #16a34a";
            this.style.borderTop = "";
          } else {
            this.style.borderTop = "2px solid #16a34a";
            this.style.borderBottom = "";
          }
        });

        // Cuando sacamos la tarjeta de encima de otra, borramos la línea
        card.addEventListener('dragleave', function() {
          this.style.borderTop = "";
          this.style.borderBottom = "";
        });

        // Cuando finalmente soltamos la tarjeta en su lugar
        card.addEventListener('drop', function(e) {
          e.preventDefault();
          this.style.borderTop = "";
          this.style.borderBottom = "";
          
          if (this === draggedItem) return;

          const bounding = this.getBoundingClientRect();
          const offset = bounding.y + (bounding.height / 2);
          
          // Mueve el elemento HTML en el DOM
          if (e.clientY - offset > 0) {
            this.after(draggedItem);
          } else {
            this.before(draggedItem);
          }
        });
      });
      // --- FIN Drag & Drop ---

      // Cargar sentimiento de noticias para cada favorito (paralelo)
      favs.forEach(async (ticker) => {
        const sentimentEl = document.getElementById(`fav-sentiment-${ticker}`);
        if (!sentimentEl) return;

        try {
          const res = await fetch(`/api/news?ticker=${encodeURIComponent(ticker)}`);
          if (!res.ok) {
            sentimentEl.textContent = '';
            return;
          }
          const payload = await res.json();
          const articles = Array.isArray(payload.articles) ? payload.articles : [];

          if (articles.length === 0) {
            sentimentEl.textContent = 'Sin noticias recientes';
            return;
          }

          // Filtrar por últimas 24 horas si hay timestamps
          const now = Math.floor(Date.now() / 1000);
          const dayAgo = now - 24 * 60 * 60;
          const recent = articles.filter(a => (a.published || 0) >= dayAgo);
          const used = recent.length ? recent : articles;

          let pos = 0, neg = 0, neu = 0;
          used.forEach(a => {
            const s = (a.sentiment || '').toString().toLowerCase();
            if (s === 'bullish') pos++;
            else if (s === 'bearish') neg++;
            else neu++;
          });

          if (pos + neg === 0) {
            // Caso 100% Neutral
            sentimentEl.innerHTML = `
              <div style="font-size: 0.85rem; font-weight: 600; color: var(--text); margin-bottom: 6px;">Neutrales</div>
              <div style="height: 6px; width: 100%; background: var(--border); border-radius: 4px; overflow: hidden;">
                <div style="width: 100%; height: 100%; background-color: #a3a3a3;"></div>
              </div>
            `;
            return;
          }

          const posPct = Math.round((pos / (pos + neg)) * 100);
          const negPct = Math.round((neg / (pos + neg)) * 100);

          let texto = '';

          if (pos > neg) {
            texto = `Mayormente positivas (${posPct}%)`;
          } else if (neg > pos) {
            texto = `Mayormente negativas (${negPct}%)`;
          } else {
            texto = `Equilibradas (${posPct}% / ${negPct}%)`;
          }

          // Inyectamos el texto usando var(--text) y la barra gráfica dividida manteniendo sus colores
          sentimentEl.innerHTML = `
            <div style="font-size: 0.85rem; font-weight: 700; color: var(--text); margin-bottom: 6px;">${texto}</div>
            <div style="height: 6px; width: 100%; background: var(--border); border-radius: 4px; display: flex; overflow: hidden;">
              <div style="width: ${posPct}%; background-color: #16a34a; transition: width 0.5s ease;" title="${posPct}% Positivas"></div>
              <div style="width: ${negPct}%; background-color: #dc2626; transition: width 0.5s ease;" title="${negPct}% Negativas"></div>
            </div>
          `;
        } catch (e) {
          sentimentEl.textContent = '';
        }
      });
    }

    // Inicializar panel al cargar la página
    renderFavoritesPanel();
    updateStarButton(currentTicker);

    // Actualizar cotizaciones de favoritos cada 5 minutos para evitar bloqueos
    setInterval(renderFavoritesPanel, 300000);

    // =============================================
    // CONTROL TÁCTIL PARA PANELES (MÓVIL)
    // =============================================
    const leftDrawerMobile = document.querySelector('.left-drawer');
    const leftHandleMobile = document.querySelector('.drawer-handle');
    const favPanelMobile = document.getElementById('favorites-panel');
    const favHandleMobile = document.querySelector('.favorites-handle');

    // Abrir/Cerrar Noticias Globales
    leftHandleMobile.addEventListener('click', (e) => {
      if (window.innerWidth <= 950) {
        e.stopPropagation();
        leftDrawerMobile.classList.toggle('open');
        favPanelMobile.classList.remove('open'); // Cierra el otro por si acaso
      }
    });

    // Abrir/Cerrar Favoritos
    favHandleMobile.addEventListener('click', (e) => {
      if (window.innerWidth <= 950) {
        e.stopPropagation();
        favPanelMobile.classList.toggle('open');
        leftDrawerMobile.classList.remove('open'); // Cierra el otro por si acaso
      }
    });

    // Cerrar paneles al tocar cualquier parte fuera de ellos
    document.addEventListener('click', (e) => {
      if (window.innerWidth <= 950) {
        if (!leftDrawerMobile.contains(e.target)) {
          leftDrawerMobile.classList.remove('open');
        }
        if (!favPanelMobile.contains(e.target)) {
          favPanelMobile.classList.remove('open');
        }
      }
    });

    function cerrarNoticia() {
      const modal = document.getElementById('modal-noticias');
      if (modal) {
        modal.style.display = 'none';
      }
    }

    window.cerrarNoticia = cerrarNoticia;
  </script>
</body>
</html>"""
        html = html.replace("__INITIAL_TICKER__", initial_ticker)
        self._send_html(html)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return
