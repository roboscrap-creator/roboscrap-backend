import os
import re
from typing import Optional, List
from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx

app = FastAPI(title="RoboScrap 3.5 Backend", version="3.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "https://kuziampmbltqdqkqyrlb.supabase.co")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")

# Cache em memória das listas M3U indexadas
INDICE_M3U: List[dict] = []


def sb_headers(custom_key: Optional[str] = None) -> dict:
    key = custom_key or SUPABASE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def detectar_qualidade(texto: str) -> str:
    t = texto.upper()
    if "4K" in t or "2160" in t or "UHD" in t:
        return "4K"
    if "1080" in t or "FHD" in t:
        return "1080p"
    if "720" in t or "HD" in t:
        return "720p"
    return "1080p"


def detectar_audio(texto: str) -> str:
    t = texto.upper()
    if "LEG" in t or "SUB" in t:
        return "Legendado"
    return "Dublado"


def detectar_categoria(grupo: str, titulo: str) -> str:
    g = f"{grupo} {titulo}".upper()
    if any(k in g for k in ["SERIE", "SÉRIE", "SEASON", "TEMPORADA", "S01", "E01"]):
        return "Série"
    if any(k in g for k in ["TV", "CANAL", "AO VIVO", "ESPORTE", "NEWS"]):
        return "Canal"
    return "Filme"


def limpar_titulo(titulo: str) -> str:
    limpo = re.sub(r"\[.*?\]|\(.*?\)", "", titulo)
    limpo = re.sub(r"\b(4K|2160P|1080P|720P|FHD|HD|SD|DUB|LEG|DUBLADO|LEGENDADO)\b", "", limpo, flags=re.I)
    return re.sub(r"\s+", " ", limpo).strip()


def parse_m3u(conteudo: str) -> List[dict]:
    itens = []
    linhas = [l.strip() for l in conteudo.splitlines() if l.strip()]
    meta_atual = None

    for linha in linhas:
        if linha.startswith("#EXTINF:"):
            grupo_match = re.search(r'group-title="([^"]*)"', linha)
            logo_match = re.search(r'tvg-logo="([^"]*)"', linha)
            nome = linha.split(",")[-1].strip() if "," in linha else "Sem Título"
            meta_atual = {
                "raw_title": nome,
                "titulo": limpar_titulo(nome),
                "grupo": grupo_match.group(1) if grupo_match else "",
                "logo": logo_match.group(1) if logo_match else "",
            }
        elif not linha.startswith("#") and meta_atual:
            raw = f"{meta_atual['grupo']} {meta_atual['raw_title']}"
            itens.append({
                "titulo": meta_atual["titulo"] or meta_atual["raw_title"],
                "categoria": detectar_categoria(meta_atual["grupo"], meta_atual["raw_title"]),
                "url_stream": linha,
                "capa_url": meta_atual["logo"],
                "resolucao": detectar_qualidade(raw),
                "tipo_audio": detectar_audio(raw),
            })
            meta_atual = None
    return itens


async def enriquecer_tmdb(client: httpx.AsyncClient, titulo: str, categoria: str, tmdb_key: str) -> dict:
    if not tmdb_key or categoria == "Canal":
        return {}
    endpoint = "tv" if categoria == "Série" else "movie"
    try:
        r = await client.get(
            f"https://api.themoviedb.org/3/search/{endpoint}",
            params={"api_key": tmdb_key, "query": titulo, "language": "pt-BR"},
            timeout=8.0,
        )
        results = r.json().get("results", [])
        if not results:
            return {}
        top = results[0]
        poster = f"https://image.tmdb.org/t/p/w500{top['poster_path']}" if top.get("poster_path") else ""
        banner = f"https://image.tmdb.org/t/p/original{top['backdrop_path']}" if top.get("backdrop_path") else ""
        data_lanc = top.get("release_date") or top.get("first_air_date") or "2026"
        return {
            "capa_url": poster,
            "banner_url": banner,
            "sinopse": top.get("overview", ""),
            "ano": int(data_lanc[:4]) if len(data_lanc) >= 4 and data_lanc[:4].isdigit() else 2026,
        }
    except Exception:
        return {}


class ImportarM3URequest(BaseModel):
    m3u_url: str
    limite: int = 30
    aprovado_direto: bool = False
    supabase_key: Optional[str] = None
    tmdb_key: Optional[str] = None


class BuscarLinkRequest(BaseModel):
    titulo: str
    m3u_url: Optional[str] = None


@app.get("/")
@app.get("/ping")
async def healthcheck():
    return {
        "status": "online",
        "service": "RoboScrap 3.5 Backend",
        "itens_indexados": len(INDICE_M3U),
    }


async def processar_importacao_m3u(req: ImportarM3URequest):
    sb_key = req.supabase_key or SUPABASE_KEY
    tmdb_key = req.tmdb_key or TMDB_API_KEY
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(req.m3u_url, timeout=25.0)
        resp.raise_for_status()
        itens = parse_m3u(resp.text)

        global INDICE_M3U
        INDICE_M3U = itens

        enviados = 0
        for item in itens[: req.limite]:
            meta_tmdb = await enriquecer_tmdb(client, item["titulo"], item["categoria"], tmdb_key)
            payload = {
                "titulo": item["titulo"],
                "categoria": item["categoria"],
                "url_stream": item["url_stream"],
                "capa_url": meta_tmdb.get("capa_url") or item["capa_url"],
                "banner_url": meta_tmdb.get("banner_url", ""),
                "sinopse": meta_tmdb.get("sinopse", ""),
                "ano": meta_tmdb.get("ano", 2026),
                "resolucao": item["resolucao"],
                "tipo_audio": item["tipo_audio"],
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
                enviados += 1


@app.post("/importar-m3u")
async def importar_m3u(req: ImportarM3URequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(processar_importacao_m3u, req)
    return {
        "ok": True,
        "mensagem": f"Importação iniciada em segundo plano (até {req.limite} itens). Acompanhe na aba Curadoria do RoboScrap 3.5.",
    }


@app.post("/buscar-link")
async def buscar_link(req: BuscarLinkRequest):
    global INDICE_M3U
    async with httpx.AsyncClient(follow_redirects=True) as client:
        if req.m3u_url:
            resp = await client.get(req.m3u_url, timeout=20.0)
            resp.raise_for_status()
            INDICE_M3U = parse_m3u(resp.text)

        termo = req.titulo.lower().strip()
        candidatos = [i for i in INDICE_M3U if termo in i["titulo"].lower()]

        for cand in candidatos[:5]:
            try:
                head = await client.head(cand["url_stream"], timeout=6.0)
                if head.status_code < 400:
                    return {"encontrado": True, "resultado": cand}
            except Exception:
                continue

        if candidatos:
            return {"encontrado": True, "resultado": candidatos[0]}

    return {"encontrado": False, "mensagem": "Nenhum link encontrado para este título nas fontes indexadas."}


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
            url = item.get("url_stream", "")
            if not url:
                continue
            ativo = False
            try:
                chk = await client.head(url, timeout=6.0)
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
    padrao_iframe = re.compile(
        r'<iframe[^>]+src=["\'](https?://[^"\']+)["\']',
        re.IGNORECASE,
    )

    encontrados = []
    vistos = set()

    def extrair_de_html(texto_html: str, origem: str):
        limpo = texto_html.replace("\\/", "/")
        for m in padrao_stream.findall(limpo):
            if m not in vistos:
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



def _limpar_nome_stream(url: str, nome_m3u: str):
    bruto = (nome_m3u or "").strip()
    if not bruto:
        slug = url.split("?")[0].rstrip("/").split("/")[-1]
        slug = re.sub(r"\.(?:m3u8|mp4|mkv|avi|ts)$", "", slug, flags=re.I)
        bruto = re.sub(r"[._-]+", " ", slug).strip()

    qual = "1080p" if re.search(r"1080|fhd", bruto + " " + url, re.I) else ("4K" if re.search(r"4k|2160|uhd", bruto + " " + url, re.I) else "720p")
    audio = "Legendado" if re.search(r"\bleg\b|legendado|sub", bruto, re.I) else "Dublado"

    if re.search(r"S\d{1,2}E\d{1,2}|\d{1,2}x\d{1,2}|temporada|epis[oó]dio", bruto, re.I):
        cat = "Série"
    elif re.search(r"\btv\b|ao vivo|fhd|hd|sd|canal|esporte|globo|sbt|record|band|premiere|combate|hbo|telecine", bruto, re.I) and not re.search(r"\(\d{4}\)", bruto):
        cat = "TV ao Vivo"
    else:
        cat = "Filme"

    limpo = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", bruto)
    limpo = re.sub(r"\b(?:S\d{1,2}E\d{1,2}|\d{1,2}x\d{1,2}|1080p|720p|480p|4k|fhd|hd|sd|dublado|legendado|dub|leg|nacional|dual|bluray|web-dl|webrip|h264|x264|x265)\b", " ", limpo, flags=re.I)
    limpo = re.sub(r"\s+", " ", limpo).strip(" -_|:")
    return (limpo or "Sem Título", cat, qual, audio)

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

    _status_lote = {"rodando": True, "mensagem": "Baixando fonte e verificando catálogo existente..."}
    headers_sb = {"apikey": sb_key, "Authorization": f"Bearer {sb_key}", "Content-Type": "application/json"}

    try:
        async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
            r_exist = await client.get(f"{sb_url}/rest/v1/catalogo_midia?select=url_stream", headers=headers_sb)
            urls_existentes = {item.get("url_stream", "").strip() for item in (r_exist.json() if r_exist.status_code == 200 else [])}

            r_src = await client.get(fonte, headers={"User-Agent": "Mozilla/5.0"})
            if r_src.status_code != 200:
                _status_lote = {"rodando": False, "mensagem": f"Erro ao baixar lista (HTTP {r_src.status_code})."}
                return

            texto = r_src.text
            itens_brutos = []
            ultimo_nome = ""
            for linha in texto.splitlines():
                l = linha.strip()
                if l.startswith("#EXTINF"):
                    ultimo_nome = l.split(",")[-1].strip()
                elif l.startswith("http"):
                    itens_brutos.append((l, ultimo_nome))
                    ultimo_nome = ""

            if not itens_brutos:
                encontrados = re.findall(r'https?://[^\s"\'<>]+\.(?:m3u8|mp4|mkv)(?:\?[^\s"\'<>]*)?', texto, flags=re.I)
                itens_brutos = [(u, "") for u in dict.fromkeys(encontrados)]

            filtro_low = req.filtro.strip().lower()
            candidatos = []
            vistos = set()
            for u, nome_m3u in itens_brutos:
                u_clean = u.strip()
                if u_clean in urls_existentes or u_clean in vistos:
                    continue
                if filtro_low and filtro_low not in u_clean.lower() and filtro_low not in nome_m3u.lower():
                    continue
                vistos.add(u_clean)
                candidatos.append((u_clean, nome_m3u))
                if req.limite > 0 and len(candidatos) >= req.limite:
                    break

            total = len(candidatos)
            if total == 0:
                _status_lote = {
                    "rodando": False,
                    "mensagem": f"Concluído: {len(itens_brutos)} links lidos na fonte, mas 0 novos (todos já estão no catálogo ou fora do filtro)."
                }
                return

            cache_tmdb = {}
            publicados = 0
            curadoria = 0
            erros_db = 0
            ultimo_erro_db = ""
            buffer_lote = []

            for idx, (u, nome_m3u) in enumerate(candidatos, 1):
                if idx % 10 == 0 or idx == 1:
                    _status_lote = {"rodando": True, "mensagem": f"Processando {idx}/{total} (Publicados: {publicados} | Curadoria: {curadoria})..."}

                titulo_limpo, categoria, qual, audio = _limpar_nome_stream(u, nome_m3u)
                chave_cache = f"{categoria}:{titulo_limpo.lower()}"

                meta = cache_tmdb.get(chave_cache)
                if meta is None and tmdb_key and titulo_limpo != "Sem Título":
                    try:
                        r_tm = await client.get(
                            "https://api.themoviedb.org/3/search/multi",
                            params={"api_key": tmdb_key, "language": "pt-BR", "query": titulo_limpo},
                            timeout=8.0,
                        )
                        if r_tm.status_code == 200:
                            res_tm = r_tm.json().get("results") or []
                            if res_tm:
                                m = res_tm[0]
                                data_str = str(m.get("release_date") or m.get("first_air_date") or "")
                                meta = {
                                    "titulo": m.get("title") or m.get("name") or titulo_limpo,
                                    "capa": f"https://image.tmdb.org/t/p/w500{m['poster_path']}" if m.get("poster_path") else "",
                                    "banner": f"https://image.tmdb.org/t/p/original{m['backdrop_path']}" if m.get("backdrop_path") else "",
                                    "sinopse": m.get("overview") or "",
                                    "ano": int(data_str[:4]) if len(data_str) >= 4 and data_str[:4].isdigit() else 2026,
                                    "achou": bool(m.get("poster_path")),
                                }
                    except Exception:
                        pass
                    if meta is None:
                        meta = {"titulo": titulo_limpo, "capa": "", "banner": "", "sinopse": "", "ano": 2026, "achou": False}
                    cache_tmdb[chave_cache] = meta
                elif meta is None:
                    meta = {"titulo": titulo_limpo, "capa": "", "banner": "", "sinopse": "", "ano": 2026, "achou": False}

                aprovado_auto = bool(req.auto_aprovar and meta["achou"])
                buffer_lote.append({
                    "titulo": meta["titulo"],
                    "categoria": categoria,
                    "url_stream": u,
                    "qualidade": qual,
                    "capa_url": meta["capa"],
                    "banner_url": meta["banner"],
                    "sinopse": meta["sinopse"],
                    "genero": "Ação, Aventura",
                    "ano": meta["ano"],
                    "aprovado": aprovado_auto,
                    "link_ativo": True,
                })

                if len(buffer_lote) >= 20 or idx == total:
                    r_ins = await client.post(f"{sb_url}/rest/v1/catalogo_midia", headers={**headers_sb, "Prefer": "return=minimal"}, json=buffer_lote)
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
    return {"ok": True, "mensagem": "Importação inteligente iniciada no servidor Render!"}

@app.get("/status-lote")
async def status_lote():
    return _status_lote
