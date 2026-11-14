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
