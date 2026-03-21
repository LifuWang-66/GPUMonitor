from __future__ import annotations

from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, engine, get_db
from app.schemas import CredentialCheckRequest, HostAccessResult, SessionResponse
from app.services.analytics import get_current_status, get_gpu_history, get_user_history
from app.services.collector import collect_live_current_status, ensure_hosts, run_collection
from app.services.ssh_client import SshCredentials, validate_host_access

settings = get_settings()
scheduler = BackgroundScheduler(timezone='UTC')


def _scheduled_collection() -> None:
    db = SessionLocal()
    try:
        run_collection(db)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        ensure_hosts(db)
    finally:
        db.close()
    scheduler.add_job(_scheduled_collection, 'interval', minutes=settings.collector_interval_minutes, id='collector', replace_existing=True)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
app.mount('/static', StaticFiles(directory='app/static'), name='static')
templates = Jinja2Templates(directory='app/templates')


def get_allowed_hosts(request: Request) -> list[str]:
    return request.session.get('accessible_hosts', [])


@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        request,
        'index.html',
        {
            'app_name': settings.app_name,
            'host_aliases': settings.hosts,
            'history_windows': settings.allowed_history_windows,
            'session_username': request.session.get('username'),
            'accessible_hosts': request.session.get('accessible_hosts', []),
        },
    )


@app.post('/api/session/access', response_model=list[HostAccessResult])
def create_access_session(payload: CredentialCheckRequest, request: Request):
    credentials = SshCredentials(username=payload.username, password=payload.password, use_agent=payload.use_agent)
    results: list[HostAccessResult] = []
    accessible_hosts: list[str] = []
    for host in settings.hosts:
        accessible, reason = validate_host_access(host['address'], credentials)
        if accessible:
            accessible_hosts.append(host['address'])
        results.append(HostAccessResult(name=host['name'], address=host['address'], accessible=accessible, reason=reason))
    if not accessible_hosts:
        raise HTTPException(status_code=400, detail='当前凭据无法访问任何 GPU 服务器。')
    request.session['username'] = payload.username
    request.session['accessible_hosts'] = accessible_hosts
    return results


@app.post('/session/access')
def create_access_session_form(
    request: Request,
    username: str = Form(...),
    password: str = Form(default=''),
    use_agent: bool = Form(default=False),
):
    create_access_session(CredentialCheckRequest(username=username, password=password or None, use_agent=use_agent), request)
    return RedirectResponse(url='/', status_code=303)


@app.post('/api/session/logout', response_model=SessionResponse)
def logout(request: Request):
    username = request.session.get('username', '')
    request.session.clear()
    return SessionResponse(username=username, accessible_hosts=[])


@app.get('/api/session', response_model=SessionResponse)
def get_session(request: Request):
    return SessionResponse(
        username=request.session.get('username', ''),
        accessible_hosts=request.session.get('accessible_hosts', []),
    )


@app.get('/api/status/current')
def api_current_status(allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    return get_current_status(db, allowed_hosts)


@app.post('/api/status/refresh')
def api_refresh_current_status(allowed_hosts: list[str] = Depends(get_allowed_hosts)):
    current_status, errors = collect_live_current_status(allowed_hosts)
    return {'current_status': current_status, 'errors': errors}


@app.get('/api/history/gpus')
def api_gpu_history(days: int = 30, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    if days not in settings.allowed_history_windows:
        raise HTTPException(status_code=400, detail='不支持的时间窗口。')
    return get_gpu_history(db, allowed_hosts, days)


@app.get('/api/history/users')
def api_user_history(days: int = 30, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    if days not in settings.allowed_history_windows:
        raise HTTPException(status_code=400, detail='不支持的时间窗口。')
    return get_user_history(db, allowed_hosts, days)


@app.post('/api/collector/run')
def api_run_collector(db: Session = Depends(get_db)):
    return {'messages': run_collection(db)}
