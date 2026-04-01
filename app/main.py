from __future__ import annotations

from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, engine, get_db
from app.models import UserProfile
from app.schemas import CredentialCheckRequest, HostAccessResult, SessionResponse, TestEmailRequest, TestEmailResponse
from app.services.analytics import get_current_status, get_gpu_history, get_user_history
from app.services.collector import ensure_hosts, get_collector_credentials, refresh_current_status_only, run_collection
from app.services.notifications import send_email
from app.services.ssh_client import SshCredentials, fetch_home_users, validate_host_access

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


def resolve_hosts_from_collector_view(username: str, fallback_hosts: list[str]) -> list[str]:
    collector_credentials = get_collector_credentials()
    if collector_credentials is None:
        return fallback_hosts

    visible_hosts: list[str] = []
    for host in settings.hosts:
        try:
            host_users = fetch_home_users(host['address'], collector_credentials)
            if username in host_users:
                visible_hosts.append(host['address'])
        except Exception:  # noqa: BLE001
            continue
    return visible_hosts or fallback_hosts


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
            'session_email': request.session.get('email'),
            'accessible_hosts': request.session.get('accessible_hosts', []),
        },
    )


@app.post('/api/session/access', response_model=list[HostAccessResult])
def create_access_session(payload: CredentialCheckRequest, request: Request, db: Session = Depends(get_db)):
    normalized_username = payload.username.strip()
    profile = db.scalar(select(UserProfile).where(UserProfile.username == normalized_username))
    input_email = (payload.email or '').strip() or None
    profile_email = (profile.email or '').strip() if profile else ''
    if profile is None:
        if not input_email:
            raise HTTPException(status_code=400, detail='Email is required the first time this user logs in.')
        profile = UserProfile(username=normalized_username, email=input_email)
        db.add(profile)
    elif not profile_email and not input_email:
        raise HTTPException(status_code=400, detail='Email is required because this user does not have an email on file.')
    elif input_email and input_email != profile_email:
        profile.email = input_email
    db.commit()

    credentials = SshCredentials(username=normalized_username, password=payload.password, use_agent=payload.use_agent)
    results: list[HostAccessResult] = []
    accessible_hosts: list[str] = []
    for host in settings.hosts:
        accessible, reason = validate_host_access(host['address'], credentials)
        if accessible:
            accessible_hosts.append(host['address'])
        results.append(HostAccessResult(name=host['name'], address=host['address'], accessible=accessible, reason=reason))
    if not accessible_hosts:
        raise HTTPException(status_code=400, detail='当前凭据无法访问任何 GPU 服务器。')
    accessible_hosts = resolve_hosts_from_collector_view(normalized_username, accessible_hosts)
    request.session['username'] = normalized_username
    request.session['email'] = profile.email
    request.session['accessible_hosts'] = accessible_hosts
    return results


@app.post('/session/access')
def create_access_session_form(
    request: Request,
    username: str = Form(...),
    email: str = Form(default=''),
    password: str = Form(default=''),
    use_agent: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    create_access_session(
        CredentialCheckRequest(username=username, email=email or None, password=password or None, use_agent=use_agent),
        request,
        db,
    )
    return RedirectResponse(url='/', status_code=303)


@app.post('/api/session/logout', response_model=SessionResponse)
def logout(request: Request):
    username = request.session.get('username', '')
    email = request.session.get('email')
    request.session.clear()
    return SessionResponse(username=username, email=email, accessible_hosts=[])


@app.get('/api/session', response_model=SessionResponse)
def get_session(request: Request):
    return SessionResponse(
        username=request.session.get('username', ''),
        email=request.session.get('email'),
        accessible_hosts=request.session.get('accessible_hosts', []),
    )


@app.get('/api/status/current')
def api_current_status(allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    return get_current_status(db, allowed_hosts)


@app.post('/api/status/refresh')
def api_refresh_current_status(allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    current_status, errors = refresh_current_status_only(db, allowed_hosts)
    return {'current_status': current_status, 'errors': errors}


@app.get('/api/history/gpus')
def api_gpu_history(days: int = 30, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    if days not in settings.allowed_history_windows:
        raise HTTPException(status_code=400, detail='不支持的时间窗口。')
    return get_gpu_history(db, allowed_hosts, days)


@app.get('/api/history/users')
def api_user_history(request: Request, days: int = 30, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    if days not in settings.allowed_history_windows:
        raise HTTPException(status_code=400, detail='不支持的时间窗口。')
    return get_user_history(db, allowed_hosts, days, viewer_username=request.session.get('username', ''))


@app.post('/api/collector/run')
def api_run_collector(db: Session = Depends(get_db)):
    return {'messages': run_collection(db)}


@app.post('/api/notifications/test-email', response_model=TestEmailResponse)
def api_test_email(payload: TestEmailRequest, request: Request, db: Session = Depends(get_db)):
    viewer = (request.session.get('username') or '').strip()
    if not viewer:
        raise HTTPException(status_code=401, detail='Please login first.')

    session_email = (request.session.get('email') or '').strip()
    target_email = (payload.to_email or session_email).strip()
    if not target_email:
        raise HTTPException(status_code=400, detail='Target email is required.')

    cc_email: str | None = None
    if payload.cc_lifu:
        lifu_profile = db.scalar(select(UserProfile).where(UserProfile.username == 'lifu'))
        if lifu_profile and (lifu_profile.email or '').strip():
            cc_email = lifu_profile.email.strip()

    subject = payload.subject or f'[TEST] {settings.app_name} notification check'
    body = payload.body or (
        f'Hello {viewer},\n\n'
        'This is a test email from GPU Monitor.\n'
        'If you received this, SMTP settings are working.\n'
    )

    success = send_email(target_email, subject, body, cc_email=cc_email)
    if not success:
        raise HTTPException(status_code=500, detail='Failed to send test email. Check SMTP settings.')
    return TestEmailResponse(success=True, to_email=target_email, cc_email=cc_email, detail='Test email sent.')
