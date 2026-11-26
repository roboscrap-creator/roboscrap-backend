import gc
import os
import re
import urllib.parse
from typing import Optional, List
from fastapi import FastAPI, HTTPException, BackgroundTasks, Header, Depends
from urllib.parse import urlparse

APP_SECRET = os.getenv("CINEMAX_APP_SECRET", "").strip()

async def verificar_app_secret(x_app_secret: Optional[str] = Header(default=None)):
    if not APP_SECRET:
        return True  # Modo compatibilidade ate voce definir CINEMAX_APP_SECRET no Render
    if x_app_secret != APP_SECRET:
        raise HTTPException(status_code=401, detail="Acesso não autorizado ao CineMax Backend.")
    return True

from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx


STREAM_EXT_REGEX = re.compile(r'(\.(?:m3u8|ts|mpd|m3u|mp4|mkv|avi)(?:\?[^\s"\'<>]*)?)$', re.IGNORECASE)
DIRECT_STREAM_REGEX = re.compile(r'https?://[^\s"\'<>]+?\.(?:m3u8|ts|mpd|m3u|mp4|mkv|avi)(?:\?[^\s"\'<>]*)?', re.IGNORECASE)

def is_valid_media_link(url: str) -> bool:
    if not url:
        return False
    u = url.strip().lower()
    if u.startswith("magnet:?"):
        return True
    if DIRECT_STREAM_REGEX.search(u):
        return True
    if any(k in u for k in ("/live/", "/stream/", "/hls/", "/dash/", "type=m3u", "output=ts", "output=m3u8")):
        return True
    return False

def detect_stream_format(url: str) -> str:
    u = (url or "").strip().lower()
    if u.startswith("magnet:?"):
        return "MAGNET"
    for ext in ("m3u8", "ts", "mpd", "m3u", "mp4", "mkv", "avi"):
        if re.search(rf'\.{ext}(?:\?|$|#|&)', u):
            return ext.upper()
    if "output=ts" in u:
        return "TS"
    if "output=m3u8" in u or "type=m3u" in u:
        return "M3U8"
    return "STREAM"


MAX_M3U_LINES = 15000
MAX_BATCH_ITEMS = 50

def stream_m3u_lines(url: str, max_lines: int = MAX_M3U_LINES):
    """Lê uma lista M3U via HTTP em streaming sem carregar o arquivo inteiro de uma vez na RAM."""
    linhas = []
    try:
        with httpx.Client(timeout=25.0, follow_redirects=True) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line:
                        continue
                    linhas.append(line.strip())
                    if len(linhas) >= max_lines:
                        break
    except Exception:
        pass
    return linhas

app = FastAPI(title="RoboScrap 4.0 Pro Backend", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kuziampmbltqdqkqyrlb.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

INDICE_M3U: List[dict] = []

DOMINIOS_MORTOS = (
    "0.0.0.0",
    "lh3.googleusercontent.com",
    "dailymotion.com/sec(",
    "staniptv",
    "ntcdn.us",
    "vstreaming.net",
    "popfilmeshd.net",
    "themediafire.club",
    "cdnv12.ec.cx",
    "cdnv18.ec.cx",
)


def sb_headers(custom_key: Optional[str] = None) -> dict:
    key = custom_key or SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def url_morta(url: str) -> bool:
    u = (url or "").strip()
    if not u:
        return True
    low = u.lower()
    if low.startswith("magnet:?"):
        return not bool(re.match(r"^magnet:\?xt=urn:btih:(?:[a-f0-9]{40}|[a-z2-7]{32})(?:&|$)", low))
    if low.startswith(("http://", "https://")):
        return any(d in low for d in DOMINIOS_MORTOS)
    return True


def extrair_info_magnet(magnet: str) -> dict:
    m_hash = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet, re.I)
    info_hash = m_hash.group(1).lower() if m_hash else ""
    parsed = urllib.parse.urlparse(magnet)
    qs = urllib.parse.parse_qs(parsed.query)
    dn = urllib.parse.unquote_plus(qs.get("dn", [""])[0]).strip()
    trackers = qs.get("tr", [])
    return {
        "hash": info_hash,
        "nome_dn": dn,
        "trackers_count": len(trackers),
        "valido": bool(info_hash),
    }


def _limpar_nome_stream(url: str, nome_m3u: str = "", grupo_m3u: str = ""):
    is_magnet = url.lower().strip().startswith("magnet:?")
    bruto = (nome_m3u or "").strip()
    grupo = (grupo_m3u or "").strip().lower()

    if not bruto and is_magnet:
        bruto = extrair_info_magnet(url)["nome_dn"]

    if not bruto:
        slug = url.split("?")[0].rstrip("/").split("/")[-1]
        slug = re.sub(r"\.(?:m3u8|mp4|mkv|avi|ts)$", "", slug, flags=re.I)
        bruto = re.sub(r"[._-]+", " ", slug).strip()

    bruto_norm = re.sub(r"[._]+", " ", bruto)
    texto_analise = f"{bruto_norm} {grupo} {url}"

    if re.search(r"4k|2160|uhd", texto_analise, re.I):
        qual = "4K"
    elif re.search(r"1080|fhd|full\s*hd|alto", texto_analise, re.I):
        qual = "1080p"
    elif re.search(r"720|\bhd\b", texto_analise, re.I):
        qual = "720p"
    elif re.search(r"480|360|\bsd\b|baixo", texto_analise, re.I):
        qual = "SD"
    else:
        qual = "1080p"

    if re.search(r"dual", texto_analise, re.I):
        audio, idiomas = "Dual Áudio", ["PT", "EN"]
    elif re.search(r"\bleg\b|legendado|sub|/\d*leg/", texto_analise, re.I):
        audio, idiomas = "Legendado", ["EN"]
    else:
        audio, idiomas = "Dublado", ["PT"]

    formato = "torrent" if is_magnet else ("m3u8" if ".m3u8" in url.lower() else ("mkv" if ".mkv" in url.lower() else "mp4"))

    m_ep = re.search(r"(?:S|T)\s*(\d{1,2})\s*[\|\-:\.\s]*E(?:P)?\s*(\d{1,3})|(\d{1,2})\s*[xX]\s*(\d{1,3})", bruto_norm, re.I)
    temp_num = int(m_ep.group(1) or m_ep.group(3)) if m_ep else None
    ep_num = int(m_ep.group(2) or m_ep.group(4)) if m_ep else None

    tem_ep = bool(m_ep or re.search(r"\bT\d{1,2}\b|temporada|epis[oó]dio|complete\s*season", bruto_norm, re.I))
    e_canal_grupo = bool(re.search(r"canais|canal|tv\b|ao vivo|esporte|abertos|variedades|24h|infantil|noticias|notícias", grupo, re.I)) and not re.search(r"filme|s[eé]rie|novela|anime|vod", grupo, re.I)
    e_canal_nome = bool(re.search(r"^(?:tv\s|canal\s|globo|sbt|record|band|rede\s?tv|espn|sportv|premiere|combate|telecine|hbo|megapix|tnt|warner|sony|axn|fox|star\s?channel|discovery|natgeo|cartoon|nickelodeon|cnn|globonews)\b", bruto_norm, re.I))

    if tem_ep or re.search(r"s[eé]rie|novela|anime", grupo, re.I):
        cat = "Série"
    elif e_canal_grupo or e_canal_nome:
        cat = "TV Ao Vivo"
    else:
        cat = "Filme"

    limpo = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", bruto_norm)
    limpo = re.sub(r"\b(?:S\d{1,2}\s*E\d{1,2}|\d{1,2}x\d{1,2}|T\d{1,2}|1080p|720p|480p|4k|2160p|fhd|hd|sd|dublado|legendado|dub|leg|nacional|dual|bluray|bdrip|web-?dl|webrip|hdrip|dvdrip|h264|x264|x265|hevc|aac|ac3|5\.1|20\d{2}|19\d{2})\b.*$", " ", limpo, flags=re.I)
    limpo = re.sub(r"\s+", " ", limpo).strip(" -_|:")
    return (limpo or "Sem Título", cat, qual, audio, idiomas, formato, temp_num, ep_num)


def parse_lista_mista(conteudo: str) -> List[dict]:
    itens = []
    linhas = [l.strip() for l in conteudo.splitlines() if l.strip()]
    meta_atual = None

    for linha in linhas:
        if linha.startswith("#EXTINF:"):
            grupo_match = re.search(r'group-title="([^"]*)"', linha, re.I)
            logo_match = re.search(r'tvg-logo="([^"]*)"', linha, re.I)
            nome = linha.split(",")[-1].strip() if "," in linha else ""
            meta_atual = {
                "raw_title": nome,
                "grupo": grupo_match.group(1) if grupo_match else "",
                "logo": logo_match.group(1) if logo_match else "",
            }
        elif linha.startswith("http://") or linha.startswith("https://") or linha.lower().startswith("magnet:?"):
            if url_morta(linha):
                meta_atual = None
                continue
            raw_title = meta_atual["raw_title"] if meta_atual else ""
            grupo = meta_atual["grupo"] if meta_atual else ""
            logo = meta_atual["logo"] if meta_atual else ""
            titulo_limpo, cat, qual, audio, idiomas, formato, temp_num, ep_num = _limpar_nome_stream(linha, raw_title, grupo)
            itens.append({
                "titulo": titulo_limpo,
                "categoria": cat,
                "url_stream": linha,
                "capa_url": logo,
                "resolucao": qual,
                "tipo_audio": audio,
                "idiomas_audio": idiomas,
                "formato": formato,
                "temporada": temp_num,
                "episodio": ep_num,
            })
            meta_atual = None
    return itens


async def buscar_imdb_sugestao(client: httpx.AsyncClient, termo: str) -> dict:
    limpo = re.sub(r"[^a-zA-Z0-9\s]", "", termo).strip().lower()
    if not limpo:
        return {}
    letra = limpo[0]
    q = urllib.parse.quote(limpo[:40])
    try:
        r = await client.get(
            f"https://v3.sg.media-imdb.com/suggestion/{letra}/{q}.json",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=6.0,
        )
        if r.status_code != 200:
            return {}
        for item in (r.json().get("d") or []):
            imdb_id = item.get("id", "")
            if imdb_id.startswith("tt"):
                img_obj = item.get("i") or {}
                return {
                    "imdb_id": imdb_id,
                    "titulo_imdb": item.get("l", ""),
                    "ano_imdb": item.get("y"),
                    "capa_imdb": img_obj.get("imageUrl", ""),
                    "tipo_imdb": "tv" if item.get("qid") in ("tvSeries", "tvMiniSeries") else "movie",
                }
    except Exception:
        pass
    return {}


async def enriquecer_tmdb_e_imdb(client: httpx.AsyncClient, query: str, categoria: str, tmdb_key: str, imdb_id_forcado: str = "") -> dict:
    if categoria == "TV Ao Vivo":
        return {}

    imdb_info = {}
    imdb_id = imdb_id_forcado.strip()
    if not imdb_id and re.match(r"^tt\d{6,10}$", query.strip(), re.I):
        imdb_id = query.strip().lower()

    if not imdb_id:
        imdb_info = await buscar_imdb_sugestao(client, query)
        imdb_id = imdb_info.get("imdb_id", "")

    resultado = {
        "imdb_id": imdb_id,
        "nota_imdb": None,
        "tmdb_id": None,
        "titulo": "",
        "capa_url": imdb_info.get("capa_imdb", ""),
        "banner_url": "",
        "sinopse": "",
        "genero": "",
        "ano": imdb_info.get("ano_imdb") or 2026,
        "media_type": imdb_info.get("tipo_imdb", ""),
        "achou": bool(imdb_info.get("capa_imdb")),
    }

    if not tmdb_key:
        return resultado

    try:
        top, media_type = None, ""
        if imdb_id:
            r_find = await client.get(
                f"https://api.themoviedb.org/3/find/{imdb_id}",
                params={"api_key": tmdb_key, "external_source": "imdb_id", "language": "pt-BR"},
                timeout=8.0,
            )
            if r_find.status_code == 200:
                df = r_find.json()
                if df.get("movie_results"):
                    top, media_type = df["movie_results"][0], "movie"
                elif df.get("tv_results"):
                    top, media_type = df["tv_results"][0], "tv"

        if top is None and query.strip():
            r_search = await client.get(
                "https://api.themoviedb.org/3/search/multi",
                params={"api_key": tmdb_key, "query": query.strip(), "language": "pt-BR"},
                timeout=8.0,
            )
            if r_search.status_code == 200:
                res_list = [x for x in (r_search.json().get("results") or []) if x.get("media_type") in ("movie", "tv")]
                if res_list:
                    top = res_list[0]
                    media_type = top.get("media_type", "movie")

        if top:
            tmdb_id = top.get("id")
            poster = f"https://image.tmdb.org/t/p/w500{top['poster_path']}" if top.get("poster_path") else resultado["capa_url"]
            banner = f"https://image.tmdb.org/t/p/original{top['backdrop_path']}" if top.get("backdrop_path") else ""
            data_lanc = str(top.get("release_date") or top.get("first_air_date") or "")
            vote = top.get("vote_average")
            generos_str = ""

            if tmdb_id and media_type:
                try:
                    direcao_str, elenco_str, classif_str = "", "", ""
                    r_det = await client.get(
                        f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                        params={
                            "api_key": tmdb_key,
                            "language": "pt-BR",
                            "append_to_response": "external_ids,credits,release_dates,content_ratings",
                        },
                        timeout=8.0,
                    )
                    if r_det.status_code == 200:
                        det = r_det.json()
                        generos_str = ", ".join([g.get("name", "") for g in (det.get("genres") or []) if g.get("name")])
                        ext_imdb = (det.get("external_ids") or {}).get("imdb_id")
                        if ext_imdb:
                            imdb_id = ext_imdb
                        cred = det.get("credits") or {}
                        if media_type == "movie":
                            dirs = [c.get("name") for c in (cred.get("crew") or []) if c.get("job") == "Director" and c.get("name")]
                        else:
                            dirs = [c.get("name") for c in (det.get("created_by") or []) if c.get("name")]
                            if not dirs:
                                dirs = [c.get("name") for c in (cred.get("crew") or []) if c.get("job") in ("Director", "Executive Producer") and c.get("name")]
                        direcao_str = ", ".join(dict.fromkeys(dirs[:2]))
                        cast_list = [c.get("name") for c in (cred.get("cast") or [])[:5] if c.get("name")]
                        elenco_str = ", ".join(cast_list)
                        if media_type == "movie":
                            for pais in ("BR", "US"):
                                for item_rd in (det.get("release_dates") or {}).get("results") or []:
                                    if item_rd.get("iso_3166_1") == pais:
                                        certs = [x.get("certification", "").strip() for x in (item_rd.get("release_dates") or []) if x.get("certification", "").strip()]
                                        if certs:
                                            classif_str = certs[0]
                                            break
                                if classif_str:
                                    break
                        else:
                            for pais in ("BR", "US"):
                                for item_cr in (det.get("content_ratings") or {}).get("results") or []:
                                    if item_cr.get("iso_3166_1") == pais and (item_cr.get("rating") or "").strip():
                                        classif_str = item_cr["rating"].strip()
                                        break
                                if classif_str:
                                    break
                except Exception:
                    pass

            sinopse_base = (top.get("overview") or "").strip()
            partes_rodape = []
            if classif_str:
                partes_rodape.append(f"Classificação: {classif_str}")
            if direcao_str:
                partes_rodape.append(f"Direção: {direcao_str}")
            if elenco_str:
                partes_rodape.append(f"Elenco: {elenco_str}")
            sinopse_final = f"{sinopse_base}\n\n{' • '.join(partes_rodape)}".strip() if partes_rodape else sinopse_base
            resultado.update({
                "imdb_id": imdb_id,
                "nota_imdb": round(float(vote), 1) if vote else None,
                "tmdb_id": tmdb_id,
                "titulo": top.get("title") or top.get("name") or "",
                "capa_url": poster,
                "banner_url": banner,
                "sinopse": sinopse_final,
                "genero": generos_str,
                "direcao": direcao_str,
                "elenco": elenco_str,
                "classificacao": classif_str,
                "ano": int(data_lanc[:4]) if len(data_lanc) >= 4 and data_lanc[:4].isdigit() else resultado["ano"],
                "media_type": media_type,
                "achou": bool(poster),
            })
    except Exception:
        pass

    return resultado


class ImportarM3URequest(BaseModel):
    m3u_url: str
    limite: int = 30
    aprovado_direto: bool = False
    supabase_key: Optional[str] = None
    tmdb_key: Optional[str] = None


class BuscarLinkRequest(BaseModel):
    titulo: str
    m3u_url: Optional[str] = None


class BuscarMetadadosReq(BaseModel):
    query: str
    categoria: str = "Filme"
    tmdb_key: Optional[str] = None


class AnalisarTorrentReq(BaseModel):
    magnet_url: str
    tmdb_key: Optional[str] = None


class ExtrairSiteReq(BaseModel):
    site_url: str


class LoteInteligenteReq(BaseModel):
    fonte_url: str
    limite: int = 100
    filtro: str = ""
    auto_aprovar: bool = True
    supabase_url: str = ""
    supabase_key: str = ""
    tmdb_key: str = ""


@app.get("/")

def parse_m3u_or_text_content(conteudo: str) -> list:
    linhas = [l.strip() for l in conteudo.splitlines() if l.strip()]
    itens = []
    meta_atual = {}
    for linha in linhas:
        if linha.startswith("#EXTM3U"):
            continue
        if linha.startswith("#EXTINF:"):
            # Extrai atributos tvg-logo, group-title e título após a última vírgula
            logo_m = re.search(r'tvg-logo="([^"]*)"', linha, re.IGNORECASE)
            group_m = re.search(r'group-title="([^"]*)"', linha, re.IGNORECASE)
            titulo = linha.split(",")[-1].strip() if "," in linha else "Canal / Mídia"
            meta_atual = {
                "titulo": titulo,
                "capa": logo_m.group(1).strip() if logo_m else "",
                "grupo": group_m.group(1).strip() if group_m else "",
            }
            continue
        if linha.startswith("#"):
            continue

        # Pode ser uma linha pura com URL ou "Nome | URL"
        url_candidata = linha
        titulo_inline = ""
        if "|" in linha and "http" in linha:
            partes = linha.split("|", 1)
            if partes[1].strip().startswith(("http://", "https://", "magnet:?")):
                titulo_inline = partes[0].strip()
                url_candidata = partes[1].strip()

        if not is_valid_media_link(url_candidata):
            # Procura links embutidos dentro de texto solto
            encontrados = DIRECT_STREAM_REGEX.findall(linha) + re.findall(r'magnet:\?xt=urn:btih:[a-zA-Z0-9]+[^\s"\'<>]*', linha)
            for u in encontrados:
                fmt = detect_stream_format(u)
                itens.append({
                    "titulo": meta_atual.get("titulo") or titulo_inline or limpar_nome_torrent(u.split("/")[-1]),
                    "url": u,
                    "formato": fmt,
                    "tipo": "tv" if fmt in ("TS", "M3U8", "MPD") else "filme",
                    "grupo": meta_atual.get("grupo", ""),
                    "capa": meta_atual.get("capa", ""),
                })
            meta_atual = {}
            continue

        fmt = detect_stream_format(url_candidata)
        titulo_final = meta_atual.get("titulo") or titulo_inline
        if not titulo_final:
            raw_name = unquote(url_candidata.split("?")[0].rstrip("/").split("/")[-1])
            titulo_final = limpar_nome_torrent(re.sub(r'\.(m3u8|ts|mpd|m3u|mp4|mkv|avi)$', '', raw_name, flags=re.IGNORECASE)) or f"Stream {fmt}"

        grupo_low = (meta_atual.get("grupo") or "").lower()
        tit_low = titulo_final.lower()
        if any(k in grupo_low for k in ("serie", "série", "series", "séries", "novela", "anime")) or re.search(r's\d{1,2}e\d{1,2}|\d{1,2}x\d{1,2}', tit_low):
            tipo = "serie"
        elif any(k in grupo_low for k in ("filme", "filmes", "movie", "movies", "vod", "cinema")) or fmt in ("MAGNET", "MP4", "MKV", "AVI"):
            tipo = "filme"
        else:
            # Extensões .ts, .m3u8 e .mpd sem marcação de VOD/Série são tratados como Canais de TV ao vivo
            tipo = "tv" if fmt in ("TS", "M3U8", "MPD", "M3U") else "filme"

        itens.append({
            "titulo": titulo_final,
            "url": url_candidata,
            "formato": fmt,
            "tipo": tipo,
            "grupo": meta_atual.get("grupo", ""),
            "capa": meta_atual.get("capa", ""),
        })
        meta_atual = {}
    return itens

@app.post("/parsear-lista-conteudo")
async def parsear_lista_conteudo(payload: dict):
    conteudo = (payload.get("conteudo") or "").strip()
    if not conteudo:
        raise HTTPException(status_code=400, detail="Conteúdo vazio")
    itens = parse_m3u_or_text_content(conteudo)
    gc.collect()
    return {"total": len(itens), "itens": itens}

@app.get("/ping")
async def healthcheck():
    return {
        "status": "online",
        "service": "RoboScrap 4.0 Pro Backend (M3U + Torrent P2P + TMDB/IMDb)",
        "itens_indexados": len(INDICE_M3U),
    }


@app.post("/buscar-metadados")
async def buscar_metadados(req: BuscarMetadadosReq):
    tmdb_key = req.tmdb_key or TMDB_API_KEY
    async with httpx.AsyncClient(follow_redirects=True) as client:
        meta = await enriquecer_tmdb_e_imdb(client, req.query, req.categoria, tmdb_key)
        return {"ok": True, "metadados": meta}


@app.post("/analisar-torrent")
async def analisar_torrent(req: AnalisarTorrentReq):
    magnet = req.magnet_url.strip()
    if not magnet.lower().startswith("magnet:?xt=urn:btih:"):
        raise HTTPException(status_code=400, detail="Link negado: obrigatório prefixo magnet:?xt=urn:btih:")
    info_mag = extrair_info_magnet(magnet)
    if not info_mag["valido"]:
        raise HTTPException(status_code=400, detail="Magnet link sem info_hash (btih) válido.")

    titulo_limpo, cat, qual, audio, idiomas, formato, temp_num, ep_num = _limpar_nome_stream(magnet)
    tmdb_key = req.tmdb_key or TMDB_API_KEY
    async with httpx.AsyncClient(follow_redirects=True) as client:
        meta = await enriquecer_tmdb_e_imdb(client, titulo_limpo, cat, tmdb_key)

    return {
        "ok": True,
        "torrent": info_mag,
        "parsed": {
            "titulo": meta.get("titulo") or titulo_limpo,
            "categoria": "Série" if meta.get("media_type") == "tv" else ("Filme" if meta.get("media_type") == "movie" else cat),
            "resolucao": qual,
            "tipo_audio": audio,
            "idiomas_audio": idiomas,
            "formato": "torrent",
            "temporada": temp_num,
            "episodio": ep_num,
        },
        "metadados": meta,
    }


async def processar_importacao_m3u(req: ImportarM3URequest):
    sb_key = req.supabase_key or SUPABASE_KEY
    tmdb_key = req.tmdb_key or TMDB_API_KEY
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(req.m3u_url, timeout=25.0)
        resp.raise_for_status()
        itens = parse_lista_mista(resp.text)
        global INDICE_M3U
        INDICE_M3U = itens

        for item in itens[: req.limite]:
            meta = await enriquecer_tmdb_e_imdb(client, item["titulo"], item["categoria"], tmdb_key)
            payload = {
                "tmdb_id": meta.get("tmdb_id"),
                "titulo": meta.get("titulo") or item["titulo"],
                "categoria": item["categoria"],
                "temporada": item.get("temporada"),
                "episodio": item.get("episodio"),
                "url_stream": item["url_stream"],
                "formato": item["formato"],
                "capa_url": meta.get("capa_url") or item["capa_url"],
                "banner_url": meta.get("banner_url", ""),
                "sinopse": meta.get("sinopse", ""),
                "genero": meta.get("genero") or "Ação, Aventura",
                "ano": meta.get("ano", 2026),
                "nota_imdb": meta.get("nota_imdb"),
                "resolucao": item["resolucao"],
                "tipo_audio": item["tipo_audio"],
                "idiomas_audio": item["idiomas_audio"],
                "aprovado": req.aprovado_direto,
                "link_ativo": True,
            }
            if sb_key and not url_morta(payload.get("url_stream", "")):
                await client.post(f"{SUPABASE_URL}/rest/v1/catalogo_midia", headers=sb_headers(sb_key), json=payload, timeout=10.0)


@app.post("/importar-m3u")
async def importar_m3u(req: ImportarM3URequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_importacao_m3u, req)
    return {"ok": True, "mensagem": f"Importação M3U/Torrent iniciada em segundo plano (até {req.limite} itens)."}


@app.post("/buscar-link")
async def buscar_link(req: BuscarLinkRequest):
    global INDICE_M3U
    async with httpx.AsyncClient(follow_redirects=True) as client:
        if req.m3u_url:
            resp = await client.get(req.m3u_url, timeout=20.0)
            resp.raise_for_status()
            INDICE_M3U = parse_lista_mista(resp.text)
        termo = req.titulo.lower().strip()
        candidatos = [i for i in INDICE_M3U if termo in i["titulo"].lower()]
        return {"ok": True, "resultados": candidatos[:8]}


@app.post("/verificar-links")
async def verificar_links(supabase_key: Optional[str] = None):
    sb_key = supabase_key or SUPABASE_KEY
    if not sb_key:
        raise HTTPException(status_code=400, detail="SUPABASE_SERVICE_KEY não configurada.")
    inativos = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(f"{SUPABASE_URL}/rest/v1/catalogo_midia?select=id,url_stream,link_ativo", headers=sb_headers(sb_key), timeout=15.0)
        for item in res.json():
            url = (item.get("url_stream") or "").strip()
            if not url:
                continue
            if url_morta(url):
                ativo = False
            elif url.lower().startswith("magnet:?xt=urn:btih:"):
                ativo = True
            else:
                try:
                    chk = await client.get(url, headers={"Range": "bytes=0-512", "User-Agent": "Mozilla/5.0"}, timeout=7.0)
                    ativo = chk.status_code < 400
                except Exception:
                    ativo = False
            if ativo != item.get("link_ativo", True):
                await client.patch(f"{SUPABASE_URL}/rest/v1/catalogo_midia?id=eq.{item['id']}", headers=sb_headers(sb_key), json={"link_ativo": ativo}, timeout=10.0)
                if not ativo:
                    inativos += 1
    return {"ok": True, "links_desativados": inativos}


@app.post("/extrair-site")
async def extrair_site(req: ExtrairSiteReq):
    url_alvo = req.site_url.strip()
    if not url_alvo.startswith("http"):
        url_alvo = "https://" + url_alvo
    headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 Chrome/124.0.0.0 Mobile Safari/537.36", "Referer": url_alvo}
    padrao_stream = re.compile(r'https?://[^\s"\'<>\\]+\.(?:m3u8|mp4|mkv)(?:\?[^\s"\'<>\\]*)?', re.I)
    padrao_magnet = re.compile(r'magnet:\?xt=urn:btih:[^\s"\'<>\\]+', re.I)
    padrao_iframe = re.compile(r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']', re.I)
    encontrados, vistos = [], set()

    def extrair_de_html(texto_html: str, origem: str):
        limpo = texto_html.replace("\\/", "/").replace("&amp;", "&")
        for m in padrao_magnet.findall(limpo):
            if m not in vistos:
                vistos.add(m)
                info_m = extrair_info_magnet(m)
                qual = "4K" if "2160" in m or "4k" in m.lower() else ("1080p" if "1080" in m else "720p")
                encontrados.append({"url_stream": m, "titulo": info_m["nome_dn"], "formato": "TORRENT (Magnet)", "qualidade": qual, "origem": origem})
        for m in padrao_stream.findall(limpo):
            if m not in vistos and not url_morta(m):
                vistos.add(m)
                qual = "4K" if "2160" in m or "4k" in m.lower() else ("1080p" if "1080" in m else ("720p" if "720" in m else "HD"))
                encontrados.append({"url_stream": m, "formato": "HLS (.m3u8)" if ".m3u8" in m.lower() else "MP4/MKV", "qualidade": qual, "origem": origem})

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            res = await client.get(url_alvo, headers=headers)
            html = res.text
            m_titulo = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.DOTALL)
            titulo_pagina = re.sub(r"\s+", " ", m_titulo.group(1)).strip() if m_titulo else url_alvo
            extrair_de_html(html, "pagina_principal")
            if not encontrados:
                for ifr in padrao_iframe.findall(html)[:3]:
                    try:
                        r_ifr = await client.get(ifr, headers={**headers, "Referer": url_alvo}, timeout=12.0)
                        extrair_de_html(r_ifr.text, "iframe_player")
                    except Exception:
                        pass
        return {"ok": True, "titulo_pagina": titulo_pagina, "total": len(encontrados), "streams": encontrados}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao ler site: {str(e)}")


_status_lote = {"rodando": False, "mensagem": "Nenhum lote executado ainda."}


async def _processar_lote_bg(req: LoteInteligenteReq):
    global _status_lote
    sb_url = (req.supabase_url or os.getenv("SUPABASE_URL", "https://kuziampmbltqdqkqyrlb.supabase.co")).rstrip("/")
    sb_key = req.supabase_key or os.getenv("SUPABASE_KEY", "")
    tmdb_key = req.tmdb_key or os.getenv("TMDB_KEY", "")
    if not sb_key:
        _status_lote = {"rodando": False, "mensagem": "Erro: SUPABASE_KEY ausente."}
        return

    fonte = req.fonte_url.strip()
    if "gist.github.com/" in fonte and "/raw" not in fonte:
        fonte = fonte.rstrip("/") + "/raw"

    _status_lote = {"rodando": True, "mensagem": "Baixando fonte (M3U/Torrent) e verificando catálogo..."}
    headers_sb = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            dados_exist, offset_db = [], 0
            while True:
                r_pag = await client.get(f"{sb_url}/rest/v1/catalogo_midia?select=url_stream&order=id.asc", headers={**headers_sb, "Range": f"{offset_db}-{offset_db+999}"})
                if r_pag.status_code not in (200, 206):
                    break
                lote_pag = r_pag.json() or []
                dados_exist.extend(lote_pag)
                if len(lote_pag) < 1000:
                    break
                offset_db += 1000

            r_cols = await client.get(f"{sb_url}/rest/v1/catalogo_midia?select=*&limit=1", headers=headers_sb)
            sample_cols = r_cols.json() if r_cols.status_code == 200 else []
            colunas_validas = set(sample_cols[0].keys()) if sample_cols else {"titulo", "categoria", "url_stream", "capa_url", "banner_url", "sinopse", "genero", "ano", "aprovado", "link_ativo"}
            urls_existentes = {item.get("url_stream", "").strip() for item in dados_exist}

            r_src = await client.get(fonte, headers={"User-Agent": "Mozilla/5.0"})
            if r_src.status_code != 200:
                _status_lote = {"rodando": False, "mensagem": f"Erro ao baixar lista (HTTP {r_src.status_code})."}
                return

            texto = r_src.text
            itens_brutos, ultimo_nome, ultimo_grupo = [], "", ""
            for linha in texto.splitlines():
                l = linha.strip()
                if l.startswith("#EXTINF"):
                    ultimo_nome = l.split(",")[-1].strip()
                    m_grp = re.search(r'group-title="([^"]*)"', l, flags=re.I)
                    ultimo_grupo = m_grp.group(1).strip() if m_grp else ""
                elif l.startswith("http") or l.lower().startswith("magnet:?"):
                    if not url_morta(l):
                        itens_brutos.append((l, ultimo_nome, ultimo_grupo))
                    ultimo_nome, ultimo_grupo = "", ""

            if not itens_brutos:
                encontrados_http = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|mkv)(?:\?[^\s"\'<>]*)?', texto, flags=re.I)
                encontrados_mag = re.findall(r'magnet:\?xt=urn:btih:[^\s"\'<>]+', texto, flags=re.I)
                todos_links = [u for u in dict.fromkeys(encontrados_mag + encontrados_http) if not url_morta(u)]
                itens_brutos = [(u, "", "") for u in todos_links]

            filtro_low = req.filtro.strip().lower()
            candidatos, vistos = [], set()
            for u, nome_m3u, grupo_m3u in itens_brutos:
                u_clean = u.strip()
                if u_clean in urls_existentes or u_clean in vistos:
                    continue
                if filtro_low and filtro_low not in u_clean.lower() and filtro_low not in nome_m3u.lower() and filtro_low not in grupo_m3u.lower():
                    continue
                vistos.add(u_clean)
                candidatos.append((u_clean, nome_m3u, grupo_m3u))
                if req.limite > 0 and len(candidatos) >= req.limite:
                    break

            total = len(candidatos)
            if total == 0:
                _status_lote = {"rodando": False, "mensagem": f"Concluído: {len(itens_brutos)} links lidos, 0 novos."}
                return

            cache_meta, publicados, curadoria, buffer_lote = {}, 0, 0, []
            for idx, (u, nome_m3u, grupo_m3u) in enumerate(candidatos, 1):
                if idx % 10 == 0 or idx == 1:
                    _status_lote = {"rodando": True, "mensagem": f"Processando {idx}/{total} (Publicados: {publicados} | Curadoria: {curadoria})..."}

                titulo_limpo, categoria, qual, audio, idiomas, formato, temp_num, ep_num = _limpar_nome_stream(u, nome_m3u, grupo_m3u)
                chave_cache = f"{categoria}:{titulo_limpo.lower()}"
                meta = cache_meta.get(chave_cache)
                if meta is None:
                    meta = await enriquecer_tmdb_e_imdb(client, titulo_limpo, categoria, tmdb_key)
                    if not meta.get("titulo"):
                        meta["titulo"] = titulo_limpo
                    cache_meta[chave_cache] = meta

                if categoria != "TV Ao Vivo":
                    if meta.get("media_type") == "tv":
                        categoria = "Série"
                    elif meta.get("media_type") == "movie":
                        categoria = "Filme"

                aprovado_auto = bool(req.auto_aprovar and meta.get("achou"))
                titulo_final = meta.get("titulo") or titulo_limpo
                item_raw = {
                    "tmdb_id": meta.get("tmdb_id"),
                    "nota_imdb": meta.get("nota_imdb"),
                    "titulo": titulo_final,
                    "categoria": categoria,
                    "serie_nome": titulo_final if categoria == "Série" else None,
                    "temporada": temp_num,
                    "episodio": ep_num,
                    "url_stream": u,
                    "formato": formato,
                    "resolucao": qual,
                    "tipo_audio": audio,
                    "idiomas_audio": idiomas,
                    "capa_url": meta.get("capa_url") or "",
                    "banner_url": meta.get("banner_url") or "",
                    "sinopse": meta.get("sinopse") or "",
                    "genero": meta.get("genero") or "Ação, Aventura",
                    "ano": meta.get("ano") or 2026,
                    "aprovado": aprovado_auto,
                    "link_ativo": True,
                }
                if not url_morta(item_raw.get("url_stream", "")):
                    buffer_lote.append({k: v for k, v in item_raw.items() if k in colunas_validas})

                if len(buffer_lote) >= 20 or idx == total:
                    r_ins = await client.post(f"{sb_url}/rest/v1/catalogo_midia", headers={**headers_sb, "Prefer": "return=minimal"}, json=buffer_lote)
                    if r_ins.status_code in (200, 201):
                        for item in buffer_lote:
                            if item["aprovado"]:
                                publicados += 1
                            else:
                                curadoria += 1
                    buffer_lote = []

            _status_lote = {"rodando": False, "mensagem": f"Lote finalizado! {publicados} publicados direto e {curadoria} em Curadoria (lidos: {len(itens_brutos)})."}
    except Exception as e:
        _status_lote = {"rodando": False, "mensagem": f"Erro no lote: {e}"}


@app.post("/importar-lote-inteligente")
async def importar_lote_inteligente(req: LoteInteligenteReq, background_tasks: BackgroundTasks):
    if _status_lote.get("rodando"):
        return {"ok": False, "mensagem": "Já existe um lote rodando no servidor."}
    background_tasks.add_task(_processar_lote_bg, req)
    return {"ok": True, "mensagem": "Importação inteligente (M3U + Torrent + TMDB/IMDb) iniciada no Render!"}


@app.get("/status-lote")
async def status_lote():
    return _status_lote


# === TORBOX CLOUD STREAMING ===
import os as _tb_os
import re as _tb_re
import time as _tb_time
import asyncio as _tb_asyncio
import httpx as _tb_httpx
from pydantic import BaseModel as _TBBaseModel
from fastapi import HTTPException as _TBHTTPException

_TB_CACHE: dict[str, dict] = {}
_TB_LOCKS: dict[str, _tb_asyncio.Lock] = {}
_TB_TTL_SECONDS = 4 * 3600  # 4 horas de cache por torrent


class _TorBoxRequest(_TBBaseModel):
    magnet: str


@app.post("/stream-torbox", dependencies=[Depends(verificar_app_secret)])
async def stream_torbox(req: _TorBoxRequest):
    api_key = _tb_os.getenv("TORBOX_API_KEY", "").strip()
    cdn_host = _tb_os.getenv("GCORE_CDN_HOST", "").strip()
    if not api_key:
        raise _TBHTTPException(status_code=500, detail="TORBOX_API_KEY nao configurada")
    if not (req.magnet or "").strip().lower().startswith("magnet:?xt=urn:btih:"):
        raise _TBHTTPException(status_code=400, detail="Link negado: obrigatório prefixo magnet:?xt=urn:btih:")

    m = _tb_re.search(r"btih:([a-zA-Z0-9]{32,40})", req.magnet, _tb_re.IGNORECASE)
    info_hash = m.group(1).lower() if m else req.magnet.strip()

    # 1. Retorna imediatamente do cache se valido (< 4h)
    agora = _tb_time.time()
    cached = _TB_CACHE.get(info_hash)
    if cached and cached["exp"] > agora:
        return {"url": cached["url"], "file_name": cached["file_name"], "source": "torbox-cache"}

    # 2. Evita multiplas requisicoes simultaneas para o mesmo filme
    lock = _TB_LOCKS.setdefault(info_hash, _tb_asyncio.Lock())
    async with lock:
        agora = _tb_time.time()
        cached = _TB_CACHE.get(info_hash)
        if cached and cached["exp"] > agora:
            return {"url": cached["url"], "file_name": cached["file_name"], "source": "torbox-cache"}

        tb_base = "https://api.torbox.app/v1/api"
        headers = {"Authorization": f"Bearer {api_key}"}

        async with _tb_httpx.AsyncClient(timeout=20.0) as client:
            add_res = await client.post(
                f"{tb_base}/torrents/createtorrent",
                headers=headers,
                data={"magnet": req.magnet, "seed": "1", "allow_zip": "false"},
            )
            add_data = add_res.json()
            if not add_data.get("success") and "ALREADY" not in str(add_data.get("detail", "")).upper():
                raise _TBHTTPException(status_code=400, detail=f"Erro TorBox: {add_data.get('detail')}")

            torrent_id = (add_data.get("data") or {}).get("torrent_id")
            file_id = None
            file_name = "video.mkv"

            for _ in range(15):
                list_res = await client.get(f"{tb_base}/torrents/mylist?bypass_cache=true", headers=headers)
                items = list_res.json().get("data") or []
                alvo = next(
                    (
                        t for t in items
                        if (torrent_id and t.get("id") == torrent_id)
                        or (info_hash and info_hash in str(t.get("hash", "")).lower())
                    ),
                    None,
                )
                if alvo and alvo.get("files"):
                    torrent_id = alvo["id"]
                    videos = [
                        f for f in alvo["files"]
                        if str(f.get("name", "")).lower().endswith((".mkv", ".mp4", ".avi", ".ts"))
                    ]
                    escolhido = max(videos or alvo["files"], key=lambda x: x.get("size", 0))
                    file_id = escolhido["id"]
                    file_name = escolhido.get("short_name") or escolhido.get("name", "video.mkv")
                    break
                await _tb_asyncio.sleep(0.8)

            if not torrent_id or file_id is None:
                raise _TBHTTPException(status_code=404, detail="Torrent ainda baixando na nuvem TorBox.")

            dl_res = await client.get(
                f"{tb_base}/torrents/requestdl",
                headers=headers,
                params={"token": api_key, "torrent_id": torrent_id, "file_id": file_id},
            )
            dl_data = dl_res.json()
            stream_url = dl_data.get("data")
            if not dl_data.get("success") or not stream_url:
                raise _TBHTTPException(status_code=502, detail="Falha ao gerar link HTTPS no TorBox")

            if cdn_host:
                stream_url = _tb_re.sub(r"^https?://[^/]+", f"https://{cdn_host}", stream_url)

            _TB_CACHE[info_hash] = {
                "url": stream_url,
                "file_name": file_name,
                "exp": _tb_time.time() + _TB_TTL_SECONDS,
            }
            return {"url": stream_url, "file_name": file_name, "source": "torbox"}


# ==========================================
# BUSCA E CONVERSAO DE LEGENDAS ONLINE
# ==========================================
import re

@app.get("/legendas/buscar", dependencies=[Depends(verificar_app_secret)])
async def buscar_legendas(
    titulo: str = "",
    tmdb_id: str = "",
    tipo: str = "movie",
    temporada: int = 0,
    episodio: int = 0,
):
    resultados = []
    imdb_id = ""
    tmdb_key = os.getenv("TMDB_API_KEY", "")

    async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
        # 1. Converte TMDB ID para IMDb ID (se disponivel) para busca exata
        if tmdb_id and tmdb_key:
            media_type = "tv" if ("serie" in tipo.lower() or "tv" in tipo.lower() or temporada > 0) else "movie"
            try:
                r_ext = await client.get(
                    f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}/external_ids",
                    params={"api_key": tmdb_key},
                )
                if r_ext.status_code == 200:
                    imdb_id = r_ext.json().get("imdb_id") or ""
            except Exception:
                pass

        # 2. Consulta catalogo aberto do OpenSubtitles v3 via IMDb ID
        if imdb_id:
            if temporada > 0 and episodio > 0:
                sub_url = f"https://opensubtitles-v3.strem.io/subtitles/series/{imdb_id}:{temporada}:{episodio}.json"
            else:
                sub_url = f"https://opensubtitles-v3.strem.io/subtitles/movie/{imdb_id}.json"
            try:
                r_sub = await client.get(sub_url)
                if r_sub.status_code == 200:
                    subs = r_sub.json().get("subtitles", [])
                    for idx, s in enumerate(subs):
                        lang = (s.get("lang") or "").lower()
                        if any(k in lang for k in ["por", "pob", "pt", "eng", "spa"]):
                            label_lang = "Português" if ("por" in lang or "pob" in lang or "pt" in lang) else ("Inglês" if "eng" in lang else "Espanhol")
                            resultados.append({
                                "id": f"os_{idx}",
                                "idioma": label_lang,
                                "codigo": lang,
                                "nome": f"{label_lang} • OpenSubtitles #{idx + 1}",
                                "url": s.get("url", ""),
                            })
            except Exception:
                pass

    return {"legendas": resultados[:25], "imdb_id": imdb_id}


@app.get("/legendas/baixar")
async def baixar_legenda_vtt(url: str):
    from fastapi.responses import Response
    host = (urlparse(url).hostname or "").lower()
    dominios_permitidos = ("opensubtitles.org", "opensubtitles-v3.strem.io", "strem.io", "subs5.strem.io")
    if not any(host == d or host.endswith("." + d) for d in dominios_permitidos):
        raise HTTPException(status_code=400, detail="Domínio de legenda não permitido.")
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code != 200:
            return Response(content="WEBVTT\n\n", media_type="text/vtt")
        try:
            texto = r.content.decode("utf-8")
        except UnicodeDecodeError:
            texto = r.content.decode("latin-1", errors="replace")

        # Converte SRT para WEBVTT caso nao comece com WEBVTT
        texto = texto.replace("\r\n", "\n").strip()
        if not texto.startswith("WEBVTT"):
            texto = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", texto)
            texto = "WEBVTT\n\n" + texto

        return Response(content=texto, media_type="text/vtt; charset=utf-8")


class _AquecerCdnLoteReq(_TBBaseModel):
    magnets: list[str] = []


@app.post("/aquecer-cdn-lote", dependencies=[Depends(verificar_app_secret)])
async def aquecer_cdn_lote(req: _AquecerCdnLoteReq, background_tasks: BackgroundTasks):
    vistos = set()
    unicos = []
    for m in req.magnets:
        raw = (m or "").strip()
        if not raw.lower().startswith("magnet:?xt=urn:btih:"):
            continue
        m_hash = re.search(r"btih:([a-zA-Z0-9]+)", raw, re.I)
        chave = m_hash.group(1).lower() if m_hash else raw.lower()
        if chave not in vistos:
            vistos.add(chave)
            unicos.append(raw)

    async def _aquecer_bg(lista_magnets: list[str]):
        for mag in lista_magnets:
            try:
                await stream_torbox(_TorBoxRequest(magnet=mag))
            except Exception:
                pass

    if unicos:
        background_tasks.add_task(_aquecer_bg, unicos)
    return {"ok": True, "enfileirados": len(unicos), "duplicados_ignorados": len(req.magnets) - len(unicos)}


# ==========================================
# PROXY TMDB PROTEGIDO (SEM EXPOR KEY NO APK)
# ==========================================
@app.get("/tmdb/details/{media_type}/{tmdb_id}", dependencies=[Depends(verificar_app_secret)])
async def tmdb_details_proxy(media_type: str, tmdb_id: int):
    if media_type not in ("movie", "tv"):
        raise HTTPException(status_code=400, detail="media_type inválido")
    tmdb_key = os.getenv("TMDB_API_KEY", "").strip()
    if not tmdb_key:
        raise HTTPException(status_code=500, detail="TMDB_API_KEY não configurada no servidor")
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(
            f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
            params={
                "api_key": tmdb_key,
                "language": "pt-BR",
                "append_to_response": "credits,external_ids",
            },
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Falha ao consultar TMDB")
        return r.json()


@app.get("/tmdb/season/{tmdb_id}/{temporada}", dependencies=[Depends(verificar_app_secret)])
async def tmdb_season_proxy(tmdb_id: int, temporada: int):
    tmdb_key = os.getenv("TMDB_API_KEY", "").strip()
    if not tmdb_key:
        raise HTTPException(status_code=500, detail="TMDB_API_KEY não configurada no servidor")
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{temporada}",
            params={"api_key": tmdb_key, "language": "pt-BR"},
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail="Falha ao consultar temporada TMDB")
        return r.json()
