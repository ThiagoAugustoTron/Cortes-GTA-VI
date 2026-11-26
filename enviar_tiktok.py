"""Login com TikTok (Login Kit) e envio de cortes via Content Posting API.

NAO TESTADO contra a API real: ajuste conforme a documentacao atual do TikTok.

Configuracao: crie tiktok_client.json ao lado deste arquivo:
  {"client_key": "...", "client_secret": "...", "redirect_uri": "http://localhost:8765/callback"}
O redirect_uri deve estar cadastrado igual no portal do TikTok.

Uso:
  python enviar_tiktok.py login            # autoriza a conta (1a vez)
  python enviar_tiktok.py login --manual   # redirect_uri https (callback.html): cola a URL final
  python enviar_tiktok.py status           # situacao de cada video enviado (rascunho, publicado, falhou)
O rodar.py envia cada corte como RASCUNHO (caixa de entrada do app; voce toca em publicar).
Em app nao auditado, a conta precisa estar como usuario de teste no portal do TikTok.
"""
import hashlib
import json
import secrets
import sys
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

API = "https://open.tiktokapis.com"


def cliente() -> dict:
    return json.loads(Path("tiktok_client.json").read_text(encoding="utf-8"))


def _receber_local(c: dict, url: str) -> dict:
    achado = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            achado.update({k: v[0] for k, v in q.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h3>Autorizado. Pode fechar esta aba.</h3>".encode())

        def log_message(self, *a):
            pass

    porta = urllib.parse.urlparse(c["redirect_uri"]).port or 80
    srv = HTTPServer(("localhost", porta), H)
    print("Abrindo o navegador para autorizar no TikTok...")
    webbrowser.open(url)
    srv.handle_request()
    return achado


def login(token: Path, direto: bool = False) -> None:
    c = cliente()
    escopos = "user.info.basic,video.upload" + (",video.publish" if direto else "")
    verifier = secrets.token_urlsafe(48)
    # TikTok usa o SHA256 do verifier em hexadecimal (nao base64url)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urllib.parse.urlencode({
        "client_key": c["client_key"], "scope": escopos, "response_type": "code",
        "redirect_uri": c["redirect_uri"], "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})

    achado = {}
    host = urllib.parse.urlparse(c["redirect_uri"]).hostname
    # redirect_uri que nao e localhost (ex.: GitHub Pages) nunca chega aqui: modo manual
    if "--manual" in sys.argv or host not in ("localhost", "127.0.0.1"):
        # redirect_uri em https (ex.: GitHub Pages): cole aqui a URL final do navegador
        print("Abrindo o navegador para autorizar no TikTok...")
        webbrowser.open(url)
        print("Depois de autorizar, copie o endereco completo da pagina que abrir e cole aqui.",
              flush=True)
        colada = input("Endereco: ").strip()
        q = urllib.parse.parse_qs(urllib.parse.urlparse(colada).query)
        achado.update({k: v[0] for k, v in q.items()})
    else:
        achado = _receber_local(c, url)
    if achado.get("state") != state or "code" not in achado:
        raise SystemExit(f"Autorizacao falhou: {achado}")

    r = requests.post(f"{API}/v2/oauth/token/", data={
        "client_key": c["client_key"], "client_secret": c["client_secret"],
        "code": achado["code"], "grant_type": "authorization_code",
        "redirect_uri": c["redirect_uri"], "code_verifier": verifier}, timeout=30)
    dados = r.json()
    if "access_token" not in dados:
        raise SystemExit(f"Erro no token: {dados}")
    token.write_text(json.dumps(dados), encoding="utf-8")
    print("Login OK. Permissoes concedidas:", dados.get("scope"))


def renovar(token: Path) -> str:
    t = json.loads(token.read_text())
    c = cliente()
    r = requests.post(f"{API}/v2/oauth/token/", data={
        "client_key": c["client_key"], "client_secret": c["client_secret"],
        "grant_type": "refresh_token", "refresh_token": t["refresh_token"]}, timeout=30).json()
    if "access_token" in r:
        token.write_text(json.dumps(r), encoding="utf-8")
        return r["access_token"]
    return t["access_token"]


def enviar_video(video: Path, titulo: str, direto: bool, token: Path) -> str:
    acesso = renovar(token)
    h = {"Authorization": f"Bearer {acesso}", "Content-Type": "application/json; charset=UTF-8"}
    tam = video.stat().st_size
    if tam > 64 * 1024 * 1024:
        raise SystemExit(f"{video.name} tem mais de 64 MB; envio em varios blocos nao implementado")
    fonte = {"source": "FILE_UPLOAD", "video_size": tam, "chunk_size": tam, "total_chunk_count": 1}
    if direto:
        info = requests.post(f"{API}/v2/post/publish/creator_info/query/", headers=h, timeout=30).json()
        erro = info.get("error", {}).get("code")
        if erro not in (None, "ok"):
            dica = (" -> falta o escopo video.publish: ative-o no portal (Sandbox) e rode "
                    "'python enviar_tiktok.py login --direto'") if erro == "scope_not_authorized" else ""
            raise SystemExit(f"Erro no creator_info: {erro} ({info['error'].get('message')}){dica}")
        niveis = info["data"]["privacy_level_options"]
        corpo = {"post_info": {"title": titulo[:150], "privacy_level": niveis[0]},
                 "source_info": fonte}
        url = f"{API}/v2/post/publish/video/init/"
    else:
        corpo = {"source_info": fonte}
        url = f"{API}/v2/post/publish/inbox/video/init/"
    r = requests.post(url, headers=h, json=corpo, timeout=30).json()
    if r.get("error", {}).get("code") not in (None, "ok"):
        raise SystemExit(f"Erro no init: {r}")
    up = r["data"]["upload_url"]
    put = requests.put(up, data=video.read_bytes(), timeout=600, headers={
        "Content-Type": "video/mp4", "Content-Length": str(tam),
        "Content-Range": f"bytes 0-{tam - 1}/{tam}"})
    put.raise_for_status()
    return r["data"]["publish_id"]


TOKEN = Path("tiktok_token.json")

SIGNIFICADO = {
    "PROCESSING_DOWNLOAD": "recebendo o video",
    "SEND_TO_USER_INBOX": "rascunho aguardando voce na caixa de entrada",
    "PUBLISH_COMPLETE": "publicado",
    "FAILED": "falhou",
}


def status(publish_id: str, token: Path = TOKEN) -> dict:
    acesso = renovar(token)
    r = requests.post(f"{API}/v2/post/publish/status/fetch/", timeout=30,
                      headers={"Authorization": f"Bearer {acesso}",
                               "Content-Type": "application/json; charset=UTF-8"},
                      json={"publish_id": publish_id}).json()
    return r.get("data") or {"status": "ERRO", "fail_reason": str(r.get("error"))}


def aguardar(publish_id: str, token: Path = TOKEN, limite_seg: int = 60) -> dict:
    """Espera o TikTok terminar de processar o video: so 'aceitou o arquivo' nao basta, ele ainda
    pode reprovar depois (formato, duracao, verificacao). Devolve o ultimo status."""
    fim = time.time() + limite_seg
    s = {}
    while time.time() < fim:
        s = status(publish_id, token)
        if s.get("status") in ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED", "ERRO"):
            break
        time.sleep(4)
    return s


def listar_status() -> None:
    """Consulta na API o estado de cada video enviado (historico.json)."""
    h = json.loads(Path("historico.json").read_text(encoding="utf-8"))         if Path("historico.json").exists() else {}
    enviados = [e for e in h.values() if e.get("tiktok_id")]
    if not enviados:
        print("Nenhum video enviado ao TikTok no historico.json.")
        return
    contagem = {}
    for e in enviados:
        s = status(e["tiktok_id"])
        cod = s.get("status", "?")
        contagem[cod] = contagem.get(cod, 0) + 1
        extra = f" ({s['fail_reason']})" if s.get("fail_reason") else ""
        print(f"[{cod}] {SIGNIFICADO.get(cod, '')}{extra} - {e['titulo'][:60]}")
    print("\nResumo:", contagem)


def enviar(video: Path, titulo: str, direto: bool = False) -> str:
    """Envia o video (rascunho na caixa de entrada do app; direto=True exige app aprovado)."""
    if not TOKEN.exists():
        raise SystemExit("TikTok sem login: rode 'python enviar_tiktok.py login'")
    return enviar_video(video, titulo[:150], direto, TOKEN)


if __name__ == "__main__":
    if sys.argv[1:2] == ["login"]:
        login(TOKEN, "--direto" in sys.argv)
    elif sys.argv[1:2] == ["status"]:
        listar_status()
    else:
        print(__doc__)
