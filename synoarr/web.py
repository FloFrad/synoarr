"""Mini interface web (http.server) + API JSON de synoarr.

Une seule page HTML embarquée (vanilla JS, CSS inline, aucun CDN) et une API JSON sous ``/api``.

Interface ``App`` attendue (fournie par ``app.py``) ::

    class App(Protocol):
        cfg: Config
        db: Database
        def run_watchlist_now(self) -> bool          # lance en arrière-plan, retourne tout de suite
                                                     # (False = déjà en cours)
        def run_process_now(self) -> bool            # idem (traitement des téléchargements)
        def search_now(self, media_id: int) -> None  # recherche d'un élément de la Watchlist, en arrière-plan
        def set_media_status(self, media_id: int, status: str) -> None  # "ignored" | "wanted" | "monitored"
        def status(self) -> dict  # JSON : version, started_at, last_watchlist_run, last_process_run,
                                  #        last_errors (list), dry_run, plex_configured, ds_configured
        indexer_manager: IndexerManager

    class IndexerManager(Protocol):   # les méthodes peuvent lever ValueError (message français) -> HTTP 400
        def list(self) -> List[dict]                 # lignes de la table indexers
        def catalog(self) -> List[dict]              # entrées CATALOG + "installed": bool
        def add_from_catalog(self, catalog_id: str) -> dict
        def add_url(self, name: str, url: str, api_key: str = "", categories: str = "") -> dict
        def test(self, indexer_id: int) -> Tuple[bool, str]
        def set_enabled(self, indexer_id: int, enabled: bool) -> None
        def delete(self, indexer_id: int) -> None

Les clés d'API ne sortent jamais de l'API : elles sont remplacées par "••••".
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from .httpclient import HttpError, redact

log = logging.getLogger(__name__)

MASK = "••••"
MAX_BODY = 1024 * 1024
_SECRET_KEYS = ("api_key", "apikey", "password", "passwd", "token", "passkey")


class HttpProblem(Exception):
    """Erreur renvoyée telle quelle au client : ``{"error": message}``."""

    def __init__(self, status: int, message: str):
        Exception.__init__(self, message)
        self.status = status
        self.message = message


def _redact_url(url: Any) -> Any:
    if not isinstance(url, str) or not url:
        return url
    out = redact(url).replace("%2A", "*")
    try:
        parts = urllib.parse.urlsplit(out)
    except ValueError:
        return out
    if "@" in parts.netloc:  # http://user:pass@host -> http://host
        out = urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[1], parts.path,
                                       parts.query, parts.fragment))
    return out


def mask_secrets(row: Any) -> Any:
    """Copie de *row* (dict) sans aucun secret : clés masquées, URLs nettoyées."""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for k in list(out):
        if k.lower() in _SECRET_KEYS:
            out[k] = MASK if out[k] else ""
        elif k.lower() in ("url", "link"):
            out[k] = _redact_url(out[k])
    return out


class WebServer:
    def __init__(self, app: Any, host: str, port: int, username: str = "", password: str = ""):
        self.app = app
        self.host = host
        self.port = port  # remplacé par le port réel après start() (utile avec port 0)
        self.username = username or ""
        self.password = password or ""
        self._httpd = None  # type: Optional[ThreadingHTTPServer]
        self._thread = None  # type: Optional[threading.Thread]
        self._routes = [
            ("GET", r"/api/status", self.api_status),
            ("GET", r"/api/media", self.api_media),
            ("GET", r"/api/media/(\d+)/episodes", self.api_episodes),
            ("POST", r"/api/media/(\d+)/search", self.api_media_search),
            ("POST", r"/api/media/(\d+)/status", self.api_media_status),
            ("GET", r"/api/indexers", self.api_indexers),
            ("POST", r"/api/indexers", self.api_indexer_add),
            ("POST", r"/api/indexers/(\d+)/test", self.api_indexer_test),
            ("POST", r"/api/indexers/(\d+)/enabled", self.api_indexer_enabled),
            ("DELETE", r"/api/indexers/(\d+)", self.api_indexer_delete),
            ("GET", r"/api/catalog", self.api_catalog),
            ("GET", r"/api/grabs", self.api_grabs),
            ("GET", r"/api/events", self.api_events),
            ("POST", r"/api/run/watchlist", self.api_run_watchlist),
            ("POST", r"/api/run/process", self.api_run_process),
        ]  # type: List[Tuple[str, str, Any]]
        self._compiled = [(m, re.compile(p + r"/?\Z"), f) for m, p, f in self._routes]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.username and self.password)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        handler = _make_handler(self)
        server_cls = ThreadingHTTPServer
        if ":" in self.host:
            class _V6(ThreadingHTTPServer):
                address_family = socket.AF_INET6
            server_cls = _V6
        self._httpd = server_cls((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                        name="synoarr-web", daemon=True)
        self._thread.start()
        log.debug("Interface web en écoute sur %s:%s%s", self.host, self.port,
                 " (authentification activée)" if self.auth_enabled else "")

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)
        self._httpd = None
        self._thread = None

    # -------------------------------------------------------------- helpers
    def check_auth(self, header: Optional[str]) -> bool:
        if not self.auth_enabled:
            return True
        if not header or not header.lower().startswith("basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError):
            return False
        user, sep, pwd = decoded.partition(":")
        ok_user = hmac.compare_digest(user.encode("utf-8"), self.username.encode("utf-8"))
        ok_pwd = hmac.compare_digest(pwd.encode("utf-8"), self.password.encode("utf-8"))
        return bool(sep) and ok_user and ok_pwd

    def dispatch(self, method: str, path: str, query: Dict[str, str], body: Any) -> Tuple[int, Any]:
        allowed = False
        for m, rx, func in self._compiled:
            match = rx.match(path)
            if not match:
                continue
            if m != method:
                allowed = True
                continue
            args = [int(g) for g in match.groups()]
            return 200, func(*args, query=query, body=body)
        if allowed:
            raise HttpProblem(405, "Méthode non autorisée")
        raise HttpProblem(404, "Ressource introuvable : %s" % path)

    def _media_or_404(self, media_id: int) -> Dict[str, Any]:
        media = self.app.db.get_media(media_id)
        if not media:
            raise HttpProblem(404, "Élément de la Watchlist introuvable (id %d)" % media_id)
        return media

    @staticmethod
    def _limit(query: Dict[str, str], default: int) -> int:
        try:
            return max(1, min(1000, int(query.get("limit", default))))
        except ValueError:
            raise HttpProblem(400, "Paramètre limit invalide")

    @staticmethod
    def _body_dict(body: Any) -> Dict[str, Any]:
        if body is None:
            return {}
        if not isinstance(body, dict):
            raise HttpProblem(400, "Objet JSON attendu")
        return body

    # ------------------------------------------------------------ endpoints
    def api_status(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        status = dict(self.app.status() or {})
        db = self.app.db
        one = db.query_one(
            "SELECT "
            "(SELECT COUNT(*) FROM media WHERE type='movie' AND on_watchlist=1) AS movies, "
            "(SELECT COUNT(*) FROM media WHERE type='show' AND on_watchlist=1) AS shows, "
            "(SELECT COUNT(*) FROM media WHERE type='movie' AND status='wanted' AND on_watchlist=1) AS movies_wanted, "
            "(SELECT COUNT(*) FROM episodes e JOIN media m ON m.id=e.media_id WHERE e.status='wanted' "
            " AND m.status='monitored' AND m.on_watchlist=1) AS episodes_wanted, "
            "(SELECT COUNT(*) FROM grabs WHERE status='sent') AS downloading, "
            "(SELECT COUNT(*) FROM indexers WHERE enabled=1) AS indexers_enabled") or {}
        status["counts"] = one
        return status

    def api_media(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        db = self.app.db
        summary = {}
        for r in db.query(
                "SELECT media_id, COUNT(*) AS total, "
                "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done, "
                "SUM(CASE WHEN status='grabbed' THEN 1 ELSE 0 END) AS grabbed, "
                "SUM(CASE WHEN status='wanted' THEN 1 ELSE 0 END) AS wanted "
                "FROM episodes GROUP BY media_id"):
            summary[r["media_id"]] = {k: int(r[k] or 0) for k in ("total", "done", "grabbed", "wanted")}
        out = []
        for m in db.list_media():
            m = dict(m)
            if m.get("type") == "show":
                m["episodes"] = summary.get(m["id"], {"total": 0, "done": 0, "grabbed": 0, "wanted": 0})
            out.append(m)
        return out

    def api_episodes(self, media_id: int, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        self._media_or_404(media_id)
        return self.app.db.list_episodes(media_id)

    def api_media_search(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        self.app.search_now(media_id)
        return {"ok": True, "message": "Recherche lancée pour « %s »" % media["title"]}

    def api_media_status(self, media_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        media = self._media_or_404(media_id)
        status = self._body_dict(body).get("status")
        if status not in ("ignored", "wanted", "monitored"):
            raise HttpProblem(400, "Statut invalide : utiliser ignored, wanted ou monitored")
        if status != "ignored":  # le statut « actif » dépend du type
            status = "monitored" if media["type"] == "show" else "wanted"
        self.app.set_media_status(media_id, status)
        return {"ok": True, "status": status}

    def api_indexers(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return [mask_secrets(r) for r in self.app.indexer_manager.list()]

    def api_catalog(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return [mask_secrets(r) for r in self.app.indexer_manager.catalog()]

    def api_indexer_add(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        data = self._body_dict(body)
        mgr = self.app.indexer_manager
        if data.get("catalog_id"):
            row = mgr.add_from_catalog(str(data["catalog_id"]))
        else:
            name = str(data.get("name") or "").strip()
            url = str(data.get("url") or "").strip()
            if not name or not url:
                raise HttpProblem(400, "Le nom et l'URL de l'indexer sont obligatoires")
            row = mgr.add_url(name, url, str(data.get("api_key") or "").strip(),
                              str(data.get("categories") or "").strip())
        return mask_secrets(row)

    def api_indexer_test(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        ok, message = self.app.indexer_manager.test(indexer_id)
        return {"ok": bool(ok), "message": message}

    def api_indexer_enabled(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        enabled = self._body_dict(body).get("enabled")
        if not isinstance(enabled, bool):
            raise HttpProblem(400, "Le champ « enabled » doit valoir true ou false")
        self.app.indexer_manager.set_enabled(indexer_id, enabled)
        return {"ok": True, "enabled": enabled}

    def api_indexer_delete(self, indexer_id: int, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        self.app.indexer_manager.delete(indexer_id)
        return {"ok": True}

    def api_grabs(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return self.app.db.query(
            "SELECT g.*, m.title AS media_title, m.type AS media_type FROM grabs g "
            "LEFT JOIN media m ON m.id = g.media_id ORDER BY g.id DESC LIMIT ?", (self._limit(query, 200),))

    def api_events(self, query: Dict[str, str], body: Any) -> List[Dict[str, Any]]:
        return self.app.db.recent_events(self._limit(query, 100))

    def api_run_watchlist(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        if self.app.run_watchlist_now() is False:
            return {"ok": True, "message": "Une synchronisation de la Watchlist est déjà en cours"}
        return {"ok": True, "message": "Synchronisation de la Watchlist lancée"}

    def api_run_process(self, query: Dict[str, str], body: Any) -> Dict[str, Any]:
        if self.app.run_process_now() is False:
            return {"ok": True, "message": "Un traitement des téléchargements est déjà en cours"}
        return {"ok": True, "message": "Traitement des téléchargements lancé"}


def _make_handler(server: WebServer) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "synoarr"
        sys_version = ""
        timeout = 60
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            log.debug("web: %s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: Any, ctype: str = "application/json; charset=utf-8",
                  extra: Optional[Dict[str, str]] = None) -> None:
            if isinstance(payload, bytes):
                data = payload
            else:
                data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _handle(self) -> None:
            try:
                self._handle_inner()
            except Exception:  # ne jamais faire tomber le serveur
                log.exception("Erreur interne de l'interface web (%s %s)", self.command, self.path.split("?")[0])
                try:
                    self._send(500, {"error": "Erreur interne du serveur (voir le journal)"})
                except Exception:  # client déjà parti
                    pass

        def _handle_inner(self) -> None:
            if not server.check_auth(self.headers.get("Authorization")):
                self._send(401, {"error": "Authentification requise"},
                           extra={"WWW-Authenticate": 'Basic realm="synoarr", charset="UTF-8"'})
                return
            parts = urllib.parse.urlsplit(self.path)
            path = parts.path or "/"
            method = "GET" if self.command == "HEAD" else self.command
            if path in ("/", "/index.html") and method == "GET":
                self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8",
                           {"Content-Security-Policy": CSP})
                return
            if not path.startswith("/api/"):
                self._send(404, {"error": "Page introuvable"})
                return
            if method in ("POST", "DELETE") and not self._same_origin():
                self._send(403, {"error": "Requête d'une autre origine refusée"})
                return
            try:
                body = self._read_json() if method in ("POST", "DELETE") else None
                query = dict(urllib.parse.parse_qsl(parts.query))
                status, payload = server.dispatch(method, path, query, body)
            except HttpProblem as exc:
                status, payload = exc.status, {"error": exc.message}
            except ValueError as exc:  # erreurs métier (indexer_manager...) en français
                status, payload = 400, {"error": str(exc)}
            except HttpError as exc:  # indexer injoignable pendant l'ajout (détection du type...)
                status, payload = 502, {"error": "Service distant injoignable : %s" % exc}
            self._send(status, payload)

        def _same_origin(self) -> bool:
            """Protection CSRF. Accepté : pas d'en-tête Origin (outils, anciens navigateurs), Origin égal à Host
            ou X-Forwarded-Host (proxy inverse DSM/nginx), ou corps JSON : un navigateur n'envoie un
            Content-Type application/json vers une autre origine qu'après une requête préalable (CORS
            preflight) que ce serveur n'accepte jamais. Un formulaire piégé (text/plain, urlencoded) est refusé."""
            origin = self.headers.get("Origin")
            if not origin:
                return True
            netloc = urllib.parse.urlsplit(origin).netloc.lower()
            hosts = [self.headers.get("Host", "")]
            hosts += (self.headers.get("X-Forwarded-Host") or "").split(",")
            if netloc and netloc in [h.strip().lower() for h in hosts if h.strip()]:
                return True
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            return ctype == "application/json"

        def _read_json(self) -> Any:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise HttpProblem(400, "En-tête Content-Length invalide")
            if length > MAX_BODY:
                raise HttpProblem(413, "Requête trop volumineuse")
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.strip():
                return None
            try:
                return json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise HttpProblem(400, "JSON invalide")

        do_GET = _handle
        do_HEAD = _handle
        do_POST = _handle
        do_DELETE = _handle

    return Handler


INDEX_JS = r"""
const $=s=>document.querySelector(s), app=$('#app');
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const LBL={wanted:'Recherché',grabbed:'En téléchargement',done:'Terminé',ignored:'Ignoré',monitored:'Suivi',skipped:'Ignoré',
  sent:'Envoyé',imported:'Importé',failed:'Échec',movie:'Film',show:'Série',torznab:'Torznab',rss:'RSS',internet_archive:'Internet Archive'};
const lbl=s=>LBL[s]||s||'';
const badge=s=>`<span class="b b-${esc(s)}">${esc(lbl(s))}</span>`;
const yn=v=>v?'<span class="b b-done">Oui</span>':'<span class="b b-failed">Non</span>';
function fdate(v){if(v==null||v==='')return 'Jamais';
  if(typeof v==='number')return new Date(v*1000).toLocaleString('fr-FR');
  if(typeof v==='object')return Object.values(v).filter(x=>x!=null&&typeof x!=='object').map(fdate).join(' · ');
  return String(v).replace('T',' ').slice(0,19)}
function size(n){if(!n)return '';const u=['o','Ko','Mo','Go','To'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return n.toFixed(i>2?1:0)+' '+u[i]}
let tab=(location.hash||'#dash').slice(1), exp=new Set(), timer=null;
async function api(p,method,body){
  const o={method:method||'GET',headers:{}};
  if(body!==undefined||o.method!=='GET'){o.headers['Content-Type']='application/json';o.body=JSON.stringify(body||{})}
  const r=await fetch('api/'+p,o);let d=null;try{d=await r.json()}catch(e){}
  if(!r.ok)throw new Error((d&&d.error)||('Erreur HTTP '+r.status));return d}
function toast(msg,bad){const t=$('#toast');t.textContent=msg;t.className='show'+(bad?' bad':'');
  clearTimeout(t._h);t._h=setTimeout(()=>t.className='',4000)}
async function act(fn,okMsg){try{const d=await fn();toast((d&&d.message)||okMsg||'OK',d&&d.ok===false);await refresh()}
  catch(e){toast(e.message,true)}}
const V={};
V.dash=async()=>{const [s,ev]=await Promise.all([api('status'),api('events?limit=8')]);const c=s.counts||{};
  const errs=(s.last_errors||[]).map(e=>`<li>${typeof e==='object'?esc(fdate(e.ts||e.at||''))+' — '+esc(e.message||e.error||JSON.stringify(e)):esc(e)}</li>`).join('');
  return `<div class="grid">
  <div class="card stat"><b>${c.movies||0}</b><span>Films suivis</span></div><div class="card stat"><b>${c.shows||0}</b><span>Séries suivies</span></div>
  <div class="card stat"><b>${(c.movies_wanted||0)+(c.episodes_wanted||0)}</b><span>Recherchés</span></div>
  <div class="card stat"><b>${c.downloading||0}</b><span>En téléchargement</span></div></div>
  <div class="card"><div class="row"><button data-run="watchlist">Synchroniser la Watchlist</button><button data-run="process">Traiter les téléchargements</button></div>
  <dl><dt>Dernière synchro Watchlist</dt><dd>${esc(fdate(s.last_watchlist_run))}</dd><dt>Dernier traitement</dt><dd>${esc(fdate(s.last_process_run))}</dd>
  <dt>Plex</dt><dd>${yn(s.plex_configured)}</dd><dt>Download Station</dt><dd>${yn(s.ds_configured)}</dd>
  <dt>Indexers actifs</dt><dd>${c.indexers_enabled||0}</dd><dt>Mode simulation</dt><dd>${s.dry_run?'<span class="b b-grabbed">Activé</span>':'Non'}</dd>
  <dt>Démarré le</dt><dd>${esc(fdate(s.started_at))}</dd><dt>Version</dt><dd>${esc(s.version)}</dd></dl></div>
  ${errs?`<div class="card err"><h3>Dernières erreurs</h3><ul>${errs}</ul></div>`:''}
  <div class="card"><h3>Activité récente</h3>${evList(ev)}</div>`};
const evList=ev=>ev.length?`<ul class="ev">${ev.map(e=>`<li class="lv-${esc(e.level)}"><time>${esc(fdate(e.ts))}</time> <span class="k">${esc(e.kind)}</span> ${esc(e.message)}</li>`).join('')}</ul>`:'<p class="mut">Aucun événement.</p>';
V.watch=async()=>{const ms=await api('media');const f=$('#wf')?$('#wf').value:'',q=$('#wq')?$('#wq').value.toLowerCase():'';
  const items=ms.filter(m=>(!f||m.type===f)&&(!q||(m.title||'').toLowerCase().includes(q)));
  const eps=await Promise.all(items.filter(m=>exp.has(m.id)).map(async m=>[m.id,await api('media/'+m.id+'/episodes').catch(()=>[])]));
  const em=Object.fromEntries(eps);
  return `<div class="row tools"><select id="wf"><option value="">Tous</option><option value="movie"${f==='movie'?' selected':''}>Films</option><option value="show"${f==='show'?' selected':''}>Séries</option></select>
  <input id="wq" type="search" placeholder="Filtrer…" value="${esc(q)}"></div>
  ${items.length?items.map(m=>{const e=m.episodes,pct=e&&e.total?Math.round(100*e.done/e.total):0,ign=m.status==='ignored';
  return `<div class="card item${m.on_watchlist?'':' off'}"><div class="it"><div><b>${esc(m.title)}</b> ${m.year?`<span class="mut">(${esc(m.year)})</span>`:''}
  <div class="meta">${badge(m.type)} ${badge(m.status)} ${m.on_watchlist?'':'<span class="mut">retiré de la Watchlist</span>'}
  ${e?`<span class="mut">${e.done}/${e.total} épisodes · ${e.wanted} recherchés${e.grabbed?' · '+e.grabbed+' en cours':''}</span>`:''}
  ${m.last_search_at?`<span class="mut">dernière recherche ${esc(fdate(m.last_search_at))}</span>`:''}</div>
  ${e&&e.total?`<div class="bar"><i style="width:${pct}%"></i></div>`:''}</div>
  <div class="acts"><button data-search="${m.id}"${ign?' disabled':''}>Rechercher</button><button class="sec" data-st="${m.id}" data-v="${ign?'wanted':'ignored'}">${ign?'Reprendre':'Ignorer'}</button>
  ${m.type==='show'?`<button class="sec" data-eps="${m.id}">${exp.has(m.id)?'Masquer':'Épisodes'}</button>`:''}</div></div>
  ${exp.has(m.id)?epTable(em[m.id]||[]):''}</div>`}).join(''):'<p class="mut card">Watchlist vide : ajoutez des films ou séries à votre Watchlist Plex puis synchronisez.</p>'}`};
function epTable(eps){if(!eps.length)return '<p class="mut">Aucun épisode connu.</p>';const by={};eps.forEach(e=>(by[e.season]=by[e.season]||[]).push(e));
  return Object.keys(by).map(s=>`<details${Object.keys(by).length<3?' open':''}><summary>Saison ${esc(s)} — ${by[s].filter(e=>e.status==='done').length}/${by[s].length}</summary>
  <table class="eps">${by[s].map(e=>`<tr><td>E${String(e.episode).padStart(2,'0')}</td><td>${esc(e.title||'')}</td><td class="mut">${esc(e.air_date||'')}</td><td>${badge(e.status)}</td></tr>`).join('')}</table></details>`).join('')}
V.idx=async()=>{const [ix,cat]=await Promise.all([api('indexers'),api('catalog')]);
  return `<div class="card"><h3>Indexers configurés</h3>${ix.length?ix.map(i=>`<div class="item it"><div><b>${esc(i.name)}</b> ${badge(i.type)} ${i.enabled?'<span class="b b-done">Actif</span>':'<span class="b b-ignored">Désactivé</span>'}
  <div class="meta mut"><span class="url">${esc(i.url)}</span>${i.api_key?' · clé '+esc(i.api_key):''}${i.categories?' · catégories '+esc(i.categories):''}</div><div class="tres" id="tr${i.id}"></div></div>
  <div class="acts"><button data-test="${i.id}">Tester</button><button class="sec" data-en="${i.id}" data-v="${i.enabled?0:1}">${i.enabled?'Désactiver':'Activer'}</button><button class="danger" data-del="${i.id}" data-n="${esc(i.name)}">Supprimer</button></div></div>`).join(''):'<p class="mut">Aucun indexer : ajoutez-en un depuis le catalogue ou par URL.</p>'}</div>
  <div class="card"><h3>Catalogue (sources légales)</h3>${cat.map(c=>`<div class="item it"><div><b>${esc(c.name)}</b> ${badge(c.type)}<div class="meta mut">${esc(c.description||'')}</div></div>
  <div class="acts">${c.installed?'<span class="b b-done">Installé</span>':`<button data-cat="${esc(c.id)}">Ajouter</button>`}</div></div>`).join('')||'<p class="mut">Catalogue vide.</p>'}</div>`};
V.hist=async()=>{const [g,ev]=await Promise.all([api('grabs'),api('events?limit=100')]);
  return `<div class="card"><h3>Téléchargements envoyés</h3>${g.length?`<div class="tw"><table><tr><th>Date</th><th>Release</th><th class="hm">Média</th><th class="hm">Indexer</th><th class="hm">Taille</th><th>Statut</th></tr>
  ${g.map(x=>`<tr><td class="nw">${esc(fdate(x.created_at))}</td><td class="brk">${esc(x.release_title)}${x.message?`<div class="mut">${esc(x.message)}</div>`:''}</td><td class="hm">${esc(x.media_title||'')}${x.season!=null?` S${String(x.season).padStart(2,'0')}${x.episode!=null?'E'+String(x.episode).padStart(2,'0'):''}`:''}</td>
  <td class="hm">${esc(x.indexer||'')}</td><td class="hm nw">${size(x.size)}</td><td>${badge(x.status)}</td></tr>`).join('')}</table></div>`:'<p class="mut">Aucun téléchargement pour le moment.</p>'}</div>
  <div class="card"><h3>Événements</h3>${evList(ev)}</div>`};
async function refresh(){clearTimeout(timer);document.querySelectorAll('nav a').forEach(a=>a.classList.toggle('on',a.hash==='#'+tab));
  try{const html=await (V[tab]||V.dash)();const ae=document.activeElement,fid=ae&&ae.id,pos=fid&&ae.selectionStart;
    app.innerHTML=html;if(fid&&$('#'+fid)){const el=$('#'+fid);el.focus();if(pos!=null&&el.setSelectionRange)el.setSelectionRange(pos,pos)}
    $('#conn').textContent='';}
  catch(e){$('#conn').textContent='⚠ '+e.message}
  timer=setTimeout(refresh,15000)}
addEventListener('hashchange',()=>{tab=(location.hash||'#dash').slice(1);app.innerHTML='<p class="mut">Chargement…</p>';refresh()});
app.addEventListener('input',e=>{if(e.target.id==='wq'||e.target.id==='wf')refresh()});
app.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;
  if(d.run)act(()=>api('run/'+d.run,'POST'));
  else if(d.search)act(()=>api('media/'+d.search+'/search','POST'));
  else if(d.st)act(()=>api('media/'+d.st+'/status','POST',{status:d.v}),'Statut mis à jour');
  else if(d.eps){const id=+d.eps;exp.has(id)?exp.delete(id):exp.add(id);refresh()}
  else if(d.test){const t=$('#tr'+d.test);t.textContent='Test en cours…';t.className='tres';
    api('indexers/'+d.test+'/test','POST').then(r=>{t.textContent=(r.ok?'✔ ':'✖ ')+r.message;t.className='tres '+(r.ok?'ok':'ko')},er=>{t.textContent='✖ '+er.message;t.className='tres ko'})}
  else if(d.en)act(()=>api('indexers/'+d.en+'/enabled','POST',{enabled:d.v==='1'}),'Indexer mis à jour');
  else if(d.del){if(confirm('Supprimer l\'indexer « '+d.n+' » ?'))act(()=>api('indexers/'+d.del,'DELETE'),'Indexer supprimé')}
  else if(d.cat)act(()=>api('indexers','POST',{catalog_id:d.cat}),'Indexer ajouté')});
$('#addf').addEventListener('submit',e=>{e.preventDefault();const f=e.target,v={};new FormData(f).forEach((x,k)=>v[k]=x);
  act(async()=>{const r=await api('indexers','POST',v);f.reset();return r},'Indexer ajouté')});
const showForm=()=>{$('#addc').hidden=tab!=='idx'};addEventListener('hashchange',showForm);showForm();
refresh();
"""

INDEX_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>synoarr</title><link rel="icon" href="data:,"><meta name="color-scheme" content="light dark">
<style>
:root{--bg:#f4f5f7;--card:#fff;--fg:#1d2330;--mut:#6b7280;--bd:#e3e6eb;--ac:#2563eb;--acf:#fff;--ok:#15803d;--okb:#dcfce7;
--ko:#b91c1c;--kob:#fee2e2;--wa:#a16207;--wab:#fef3c7;--inb:#e0e7ff;--inf:#3730a3;--neb:#eceff3}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1b1f26;--fg:#e6e8eb;--mut:#9aa3af;--bd:#2c323c;--ac:#3b82f6;
--ok:#4ade80;--okb:#14321f;--ko:#f87171;--kob:#3b1717;--wa:#facc15;--wab:#3a2f0b;--inb:#1e2550;--inf:#a5b4fc;--neb:#262b33}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
header{background:var(--card);border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:2}
.hd{max-width:1000px;margin:auto;padding:10px 16px 0;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
h1{font-size:18px;margin:0 8px 8px 0}h1 span{color:var(--ac)}#conn{color:var(--ko);font-size:13px;margin-bottom:8px}
nav{display:flex;gap:2px;overflow-x:auto;flex:1 1 auto;scrollbar-width:none;align-self:flex-end}
nav a{padding:8px 12px;color:var(--mut);text-decoration:none;border-bottom:2px solid transparent;white-space:nowrap}
nav a.on{color:var(--fg);border-color:var(--ac);font-weight:600}
main{max-width:1000px;margin:auto;padding:16px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px;margin-bottom:14px}
h3{margin:0 0 10px;font-size:15px}.mut{color:var(--mut)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:14px;margin-bottom:14px}.grid .card{margin:0}
.stat b{display:block;font-size:26px}.stat span{color:var(--mut);font-size:13px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.tools{margin-bottom:14px}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 16px;margin:14px 0 0}dt{color:var(--mut)}dd{margin:0}
button{font:inherit;font-size:14px;border:1px solid var(--ac);background:var(--ac);color:var(--acf);border-radius:7px;padding:6px 12px;cursor:pointer}
button.sec{background:transparent;color:var(--ac)}button.danger{background:transparent;color:var(--ko);border-color:var(--ko)}
button:disabled{opacity:.45;cursor:default}button:hover:not(:disabled){filter:brightness(1.1)}
input,select{font:inherit;font-size:14px;padding:6px 10px;border:1px solid var(--bd);border-radius:7px;background:var(--card);color:var(--fg);min-width:0}
#wq{flex:1}
.b{display:inline-block;font-size:12px;padding:1px 8px;border-radius:99px;background:var(--neb);color:var(--mut);white-space:nowrap}
.b-done,.b-imported,.b-monitored{background:var(--okb);color:var(--ok)}.b-failed{background:var(--kob);color:var(--ko)}
.b-grabbed,.b-sent{background:var(--wab);color:var(--wa)}.b-wanted{background:var(--inb);color:var(--inf)}
.item{padding:10px 0;border-top:1px solid var(--bd)}.card.item{padding:12px 16px;border-top:1px solid var(--bd)}
.card>.item:first-of-type{border-top:0}.off{opacity:.6}
.it{display:flex;justify-content:space-between;gap:10px;align-items:flex-start;flex-wrap:wrap}.it>div:first-child{flex:1;min-width:200px}
.meta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:4px;font-size:13px}
.url{word-break:break-all}.acts{display:flex;gap:6px;flex-wrap:wrap}
.bar{height:5px;background:var(--neb);border-radius:9px;margin-top:8px;overflow:hidden;max-width:320px}.bar i{display:block;height:100%;background:var(--ok)}
.tres{font-size:13px;margin-top:4px}.tres.ok{color:var(--ok)}.tres.ko{color:var(--ko)}
details{margin-top:10px}summary{cursor:pointer;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}td,th{text-align:left;padding:6px;border-top:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-weight:600;border-top:0}table.eps{table-layout:fixed}.eps td:first-child{width:44px}.eps td:nth-child(3){width:96px}.eps td:last-child{width:150px}.tw{overflow-x:auto}.nw{white-space:nowrap}.brk{word-break:break-word}
.ev{list-style:none;margin:0;padding:0;font-size:13px}.ev li{padding:5px 0;border-top:1px solid var(--bd)}.ev li:first-child{border:0}
.ev time{color:var(--mut)}.k{font-weight:600}.lv-ERROR{color:var(--ko)}.lv-WARNING{color:var(--wa)}
.err{border-color:var(--ko)}.err ul{margin:0;padding-left:18px;color:var(--ko);font-size:13px}
form.add{display:grid;grid-template-columns:1fr 1fr;gap:8px}form.add .full{grid-column:1/-1}
#toast{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:9px 16px;
border-radius:8px;opacity:0;pointer-events:none;transition:opacity .2s;max-width:90vw;font-size:14px}#toast.show{opacity:1}#toast.bad{background:var(--ko);color:#fff}
@media (max-width:600px){.hm{display:none}nav{width:100%}nav a{padding:8px 7px;font-size:14px}.hd{padding-top:8px}h1{margin-bottom:2px}.eps td:nth-child(3){display:none}.eps td:last-child{width:auto;text-align:right}form.add{grid-template-columns:1fr}main{padding:12px}.it>div:first-child{min-width:100%}}
</style></head><body>
<header><div class="hd"><h1><span>syno</span>arr</h1>
<nav><a href="#dash">Tableau de bord</a><a href="#watch">Watchlist</a><a href="#idx">Indexers</a><a href="#hist">Historique</a></nav><div id="conn"></div></div></header>
<main><div id="app"><p class="mut">Chargement…</p></div>
<div class="card" id="addc" hidden><h3>Ajouter un indexer par URL</h3>
<form class="add" id="addf" autocomplete="off"><input name="name" placeholder="Nom" required><input name="url" placeholder="URL (Torznab, RSS…)" required>
<input name="api_key" type="password" placeholder="Clé d'API (facultatif)" autocomplete="new-password"><input name="categories" placeholder="Catégories (ex. 2000,5000)">
<div class="full"><button type="submit">Ajouter</button></div></form></div></main>
<div id="toast" role="status"></div>
<script>""" + INDEX_JS + """</script></body></html>
"""

CSP = ("default-src 'none'; script-src 'sha256-%s'; style-src 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
       % base64.b64encode(hashlib.sha256(INDEX_JS.encode("utf-8")).digest()).decode("ascii"))
