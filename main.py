import os
import re
import urllib.parse
from typing import Optional, List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

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
    u = url.lower().strip()
    if not u:
        return True
    if u.startswith("magnet:?"):
        return "btih:" not in u
    return any(d in u for d in DOMINIOS_MORTOS)


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
        info_mag = extrair_info_magnet(url)
        bruto = info_mag["nome_dn"]

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
        audio = "Dual Áudio"
        idiomas = ["PT", "EN"]
    elif re.search(r"\bleg\b|legendado|sub|/\d*leg/", texto_analise, re.I):
        audio = "Legendado"
        idiomas = ["EN"]
    elif re.search(r"\bdub\b|dublado|nacional|pt-?br", texto_analise, re.I):
        audio = "Dublado"
        idiomas = ["PT"]
    else:
        audio = "Dublado"
        idiomas = ["PT"]

    if is_magnet:
        formato = "torrent"
    elif ".m3u8" in url.lower():
        formato = "m3u8"
    elif ".mkv" in url.lower():
        formato = "mkv"
    else:
        formato = "mp4"

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
    """Consulta a API pública de sugestões do IMDb (sem precisar de chave paga) para obter imdb_id, capa e ano."""
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
        d_list = r.json().get("d") or []
        for item in d_list:
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


async def enriquecer_tmdb_e_imdb(
    client: httpx.AsyncClient,
    query: str,
    categoria: str,
    tmdb_key: str,
    imdb_id_forcado: str = "",
) -> dict:
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
        top = None
        media_type = ""

        # 1. Se temos imdb_id (tt...), busca exata via /find no TMDB
        if imdb_id:
            r_find = await client.get(
                f"https://api.themoviedb.org/3/find/{imdb_id}",
                params={"api_key": tmdb_key, "external_source": "imdb_id", "language": "pt-BR"},
                timeout=8.0,
            )
            if r_find.status_code == 200:
                df = r_find.json()
                if df.get("movie_results"):
                    top = df["movie_results"][0]
                    media_type = "movie"
                elif df.get("tv_results"):
                    top = df["tv_results"][0]
                    media_type = "tv"

        # 2. Fallback por título no /search/multi
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

            # Busca detalhes completos (gêneros + external_ids para garantir imdb_id)
            generos_str = ""
            if tmdb_id and media_type:
                try:
                    r_det = await client.get(
                        f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}",
                        params={"api_key": tmdb_key, "language": "pt-BR", "append_to_response": "external_ids"},
                        timeout=8.0,
                    )
                    if r_det.status_code == 200:
                        det = r_det.json()
                        gens = [g.get("name", "") for g in (det.get("genres") or []) if g.get("name")]
                        generos_str = ", ".join(gens)
                        ext_imdb = (det.get("external_ids") or {}).get("imdb_id")
                        if ext_imdb:
                            imdb_id = ext_imdb
                except Exception:
                    pass

            resultado.update({
                "imdb_id": imdb_id,
                "nota_imdb": round(float(vote), 1) if vote else None,
                "tmdb_id": tmdb_id,
                "titulo": top.get("title") or top.get("name") or "",
                "capa_url": poster,
                "banner_url": banner,
                "sinopse": top.get("overview") or "",
                "genero": generos_str,
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


@app.get("/")
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
    if not magnet.lower().startswith("magnet:?"):
        raise HTTPException(status_code=400, detail="Link inválido: deve começar com magnet:?")
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
            if sb_key:
                await client.post(
                    f"{SUPABASE_URL}/rest/v1/catalogo_midia",
                    headers=sb_headers(sb_key),
                    json=payload,
                    timeout=10.0,
                )


@app.post("/importar-m3u")
async def importar_m3u(req: ImportarM3URequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_importacao_m3u, req)
    return {
        "ok": True,
        "mensagem": f"Importação M3U/Torrent iniciada em segundo plano (até {req.limite} itens).",
    }


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
        validos = []
        for cand in candidatos[:8]:
            u = cand["url_stream"]
            if u.lower().startswith("magnet:?"):
                validos.append(cand)
                continue
            try:
                head = await client.get(u, headers={"Range": "bytes=0-256", "User-Agent": "Mozilla/5.0"}, timeout=6.0)
                if head.status_code < 400:
                    validos.append(cand)
            except Exception:
                continue

        return {"ok": True, "resultados": validos or candidatos[:5]}


@app.post("/verificar-links")
async def verificar_links(supabase_key: Optional[str] = None):
    sb_key = supabase_key or SUPABASE_KEY
    if not sb_key:
        raise HTTPException(status_code=400, detail="SUPABASE_SERVICE_KEY não configurada.")

    inativos = 0
    async with httpx.AsyncClient(follow_redirects=True) as client:
        res = await client.get(
            f"{SUPABASE_URL}/rest/v1/catalogo_midia?select=id,url_stream,link_ativo",
            headers=sb_headers(sb_key),
            timeout=15.0,
        )
        itens = res.json()
        for item in itens:
            url = (item.get("url_stream") or "").strip()
            if not url:
                continue
            if url.lower().startswith("magnet:?"):
                ativo = "btih:" in url.lower()
            elif url_morta(url):
                ativo = False
            else:
                try:
                    chk = await client.get(
                        url,
                        headers={"Range": "bytes=0-512", "User-Agent": "Mozilla/5.0"},
                        timeout=7.0,
                    )
                    ativo = chk.status_code < 400
                except Exception:
                    ativo = False

            if ativo != item.get("link_ativo", True):
                await client.patch(
                    f"{SUPABASE_URL}/rest/v1/catalogo_midia?id=eq.{item['id']}",
                    headers=sb_headers(sb_key),
                    json={"link_ativo": ativo},
                    timeout=10.0,
                )
                if not ativo:
                    inativos += 1

    return {"ok": True, "links_desativados": inativos}


class ExtrairSiteReq(BaseModel):
    site_url: str


@app.post("/extrair-site")
async def extrair_site(req: ExtrairSiteReq):
    url_alvo = req.site_url.strip()
    if not url_alvo.startswith("http"):
        url_alvo = "https://" + url_alvo

    headers = {
        "User-Agent": "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
        "Referer": url_alvo,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    padrao_stream = re.compile(
        r'https?://[^\s"\'<>\\]+\.(?:m3u8|mp4|mkv)(?:\?[^\s"\'<>\\]*)?',
        re.IGNORECASE,
    )
    padrao_magnet = re.compile(
        r'magnet:\?xt=urn:btih:[^\s"\'<>\\]+',
        re.IGNORECASE,
    )
    padrao_iframe = re.compile(
        r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']',
        re.IGNORECASE,
    )

    encontrados = []
    vistos = set()

    def extrair_de_html(texto_html: str, origem: str):
        limpo = texto_html.replace("\\/", "/").replace("&amp;", "&")
        for m in padrao_magnet.findall(limpo):
            if m not in vistos:
                vistos.add(m)
                info_m = extrair_info_magnet(m)
                qual = "4K" if "2160" in m or "4k" in m.lower() else ("1080p" if "1080" in m else "720p")
                encontrados.append({
                    "url_stream": m,
                    "titulo": info_m["nome_dn"],
                    "formato": "TORRENT (Magnet)",
                    "qualidade": qual,
                    "origem": origem,
                })
        for m in padrao_stream.findall(limpo):
            if m not in vistos and not url_morta(m):
                vistos.add(m)
                qual = "4K" if "2160" in m or "4k" in m.lower() else ("1080p" if "1080" in m else ("720p" if "720" in m else "HD"))
                encontrados.append({
                    "url_stream": m,
                    "formato": "HLS (.m3u8)" if ".m3u8" in m.lower() else "MP4/MKV",
                    "qualidade": qual,
                    "origem": origem,
                })

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
            res = await client.get(url_alvo, headers=headers)
            html = res.text

            m_titulo = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
            titulo_pagina = re.sub(r"\s+", " ", m_titulo.group(1)).strip() if m_titulo else url_alvo

            extrair_de_html(html, "pagina_principal")

            if not encontrados:
                iframes = padrao_iframe.findall(html)[:3]
                for ifr in iframes:
                    try:
                        r_ifr = await client.get(ifr, headers={**headers, "Referer": url_alvo}, timeout=12.0)
                        extrair_de_html(r_ifr.text, "iframe_player")
                    except Exception:
                        pass

        return {
            "ok": True,
            "titulo_pagina": titulo_pagina,
            "total": len(encontrados),
            "streams": encontrados,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao ler site: {str(e)}")


class LoteInteligenteReq(BaseModel):
    fonte_url: str
    limite: int = 100
    filtro: str = ""
    auto_aprovar: bool = True
    supabase_url: str = ""
    supabase_key: str = ""
    tmdb_key: str = ""


_status_lote = {"rodando": False, "mensagem": "Nenhum lote executado ainda."}


async def _processar_lote_bg(req: LoteInteligenteReq):
    global _status_lote
    sb_url = (req.supabase_url or os.getenv("SUPABASE_URL", "https://kuziampmbltqdqkqyrlb.supabase.co")).rstrip("/")
    sb_key = req.supabase_key or os.getenv("SUPABASE_KEY", "")
    tmdb_key = req.tmdb_key or os.getenv("TMDB_KEY", "")

    if not sb_key:
        _status_lote = {"rodando": False, "mensagem": "Erro: SUPABASE_KEY ausente. Salve suas chaves na aba Chaves."}
        return

    fonte = req.fonte_url.strip()
    if "gist.github.com/" in fonte and "/raw" not in fonte:
        fonte = fonte.rstrip("/") + "/raw"

    _status_lote = {"rodando": True, "mensagem": "Baixando fonte (M3U/Torrent) e verificando catálogo..."}
    headers_sb = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            dados_exist = []
            offset_db = 0
            while True:
                r_pag = await client.get(
                    f"{sb_url}/rest/v1/catalogo_midia?select=url_stream&order=id.asc",
                    headers={**headers_sb, "Range": f"{offset_db}-{offset_db+999}"},
                )
                if r_pag.status_code not in (200, 206):
                    break
                lote_pag = r_pag.json() or []
                dados_exist.extend(lote_pag)
                if len(lote_pag) < 1000:
                    break
                offset_db += 1000

            r_cols = await client.get(f"{sb_url}/rest/v1/catalogo_midia?select=*&limit=1", headers=headers_sb)
            sample_cols = r_cols.json() if r_cols.status_code == 200 else []
            colunas_validas = set(sample_cols[0].keys()) if sample_cols else {
                "titulo", "categoria", "url_stream", "capa_url", "banner_url", "sinopse", "genero", "ano", "aprovado", "link_ativo"
            }
            urls_existentes = {item.get("url_stream", "").strip() for item in dados_exist}

            r_src = await client.get(fonte, headers={"User-Agent": "Mozilla/5.0"})
            if r_src.status_code != 200:
                _status_lote = {"rodando": False, "mensagem": f"Erro ao baixar lista (HTTP {r_src.status_code})."}
                return

            texto = r_src.text
            itens_brutos = []
            ultimo_nome = ""
            ultimo_grupo = ""
            for linha in texto.splitlines():
                l = linha.strip()
                if l.startswith("#EXTINF"):
                    ultimo_nome = l.split(",")[-1].strip()
                    m_grp = re.search(r'group-title="([^"]*)"', l, flags=re.I)
                    ultimo_grupo = m_grp.group(1).strip() if m_grp else ""
                elif l.startswith("http") or l.lower().startswith("magnet:?"):
                    if not url_morta(l):
                        itens_brutos.append((l, ultimo_nome, ultimo_grupo))
                    ultimo_nome = ""
                    ultimo_grupo = ""

            if not itens_brutos:
                encontrados_http = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|mkv)(?:\?[^\s"\'<>]*)?', texto, flags=re.I)
                encontrados_mag = re.findall(r'magnet:\?xt=urn:btih:[^\s"\'<>]+', texto, flags=re.I)
                todos_links = [u for u in dict.fromkeys(encontrados_mag + encontrados_http) if not url_morta(u)]
                itens_brutos = [(u, "", "") for u in todos_links]

            filtro_low = req.filtro.strip().lower()
            candidatos = []
            vistos = set()
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
                _status_lote = {
                    "rodando": False,
                    "mensagem": f"Concluído: {len(itens_brutos)} links válidos lidos, 0 novos (já estão no catálogo ou fora do filtro).",
                }
                return

            cache_meta = {}
            publicados = 0
            curadoria = 0
            erros_db = 0
            ultimo_erro_db = ""
            buffer_lote = []

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
                buffer_lote.append({k: v for k, v in item_raw.items() if k in colunas_validas})

                if len(buffer_lote) >= 20 or idx == total:
                    r_ins = await client.post(
                        f"{sb_url}/rest/v1/catalogo_midia",
                        headers={**headers_sb, "Prefer": "return=minimal"},
                        json=buffer_lote,
                    )
                    if r_ins.status_code in (200, 201):
                        for item in buffer_lote:
                            if item["aprovado"]:
                                publicados += 1
                            else:
                                curadoria += 1
                    else:
                        erros_db += len(buffer_lote)
                        ultimo_erro_db = f"HTTP {r_ins.status_code}: {r_ins.text[:120]}"
                    buffer_lote = []

            msg_final = f"Lote finalizado! {publicados} publicados direto e {curadoria} em Curadoria (lidos: {len(itens_brutos)})."
            if erros_db > 0:
                msg_final += f" Falhas ao salvar ({erros_db}): {ultimo_erro_db}"
            _status_lote = {"rodando": False, "mensagem": msg_final}
    except Exception as e:
        _status_lote = {"rodando": False, "mensagem": f"Erro no lote: {e}"}


@app.post("/importar-lote-inteligente")
async def importar_lote_inteligente(req: LoteInteligenteReq, background_tasks: BackgroundTasks):
    if _status_lote.get("rodando"):
        return {"ok": False, "mensagem": "Já existe um lote rodando no servidor. Aguarde concluir."}
    background_tasks.add_task(_processar_lote_bg, req)
    return {"ok": True, "mensagem": "Importação inteligente (M3U + Torrent + TMDB/IMDb) iniciada no Render!"}


@app.get("/status-lote")
async def status_lote():
    return _status_lote
