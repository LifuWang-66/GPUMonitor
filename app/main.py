from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, commit_with_retry, engine, get_db
from app.models import Host, UserProfile
from app.schemas import (
    CredentialCheckRequest,
    HostAccessResult,
    SessionResponse,
    TestEmailRequest,
    TestEmailResponse,
    TestPolicyEmailRequest,
    TestPolicyEmailResponse,
)
from app.services.analytics import get_current_status, get_gpu_history, get_user_history
from app.services.collector import build_notification_email, ensure_hosts, get_collector_credentials, refresh_current_status_only, run_collection
from app.services.notifications import send_email
from app.services.ssh_client import SshCredentials, close_collector_connections, fetch_home_users, validate_host_access

settings = get_settings()
scheduler = BackgroundScheduler(timezone='UTC')
ADMIN_USERNAMES = {'lifu', 'panzhou'}


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
        close_collector_connections()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_credentials=True, allow_methods=['*'], allow_headers=['*'])
BACKEND_ROOT = Path(__file__).resolve().parent
app.mount('/static', StaticFiles(directory=BACKEND_ROOT / 'static'), name='static')
templates = Jinja2Templates(directory=str(BACKEND_ROOT / 'templates'))
SITE_ROOT = Path(__file__).resolve().parents[3]

if (SITE_ROOT / 'assets').exists():
    app.mount('/assets', StaticFiles(directory=SITE_ROOT / 'assets'), name='site-assets')
if (SITE_ROOT / 'css').exists():
    app.mount('/css', StaticFiles(directory=SITE_ROOT / 'css'), name='site-css')
if (SITE_ROOT / 'js').exists():
    app.mount('/js', StaticFiles(directory=SITE_ROOT / 'js'), name='site-js')
if (SITE_ROOT / 'images').exists():
    app.mount('/images', StaticFiles(directory=SITE_ROOT / 'images'), name='site-images')
if (SITE_ROOT / 'fonts').exists():
    app.mount('/fonts', StaticFiles(directory=SITE_ROOT / 'fonts'), name='site-fonts')
if (SITE_ROOT / 'SMU').exists():
    app.mount('/SMU', StaticFiles(directory=SITE_ROOT / 'SMU', html=True), name='site-smu')
if (SITE_ROOT / 'nus').exists():
    app.mount('/nus', StaticFiles(directory=SITE_ROOT / 'nus', html=True), name='site-nus')


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
    del request
    return FileResponse(SITE_ROOT / 'index.html')


@app.get('/gpu-monitor/login', response_class=HTMLResponse)
def gpu_monitor_login_page(request: Request):
    request.session.clear()
    return templates.TemplateResponse(
        request,
        'login.html',
        {
            'app_name': settings.app_name,
        },
    )


@app.get('/gpu-monitor/email', response_class=HTMLResponse)
def gpu_monitor_email_page(request: Request):
    pending_username = (request.session.get('pending_username') or '').strip()
    if not pending_username:
        return RedirectResponse(url='/gpu-monitor/login', status_code=303)
    return templates.TemplateResponse(
        request,
        'email.html',
        {
            'app_name': settings.app_name,
            'pending_username': pending_username,
        },
    )


@app.get('/gpu-monitor', response_class=HTMLResponse)
def gpu_monitor_page(request: Request):
    if not request.session.get('username') or not request.session.get('accessible_hosts'):
        return RedirectResponse(url='/gpu-monitor/login', status_code=303)
    return templates.TemplateResponse(
        request,
        'index.html',
        {
            'app_name': settings.app_name,
            'history_windows': settings.allowed_history_windows,
            'session_username': request.session.get('username'),
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
    commit_with_retry(db)

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
    normalized_username = username.strip()
    normalized_email = email.strip()
    profile = db.scalar(select(UserProfile).where(UserProfile.username == normalized_username))
    profile_email = (profile.email or '').strip() if profile else ''

    if normalized_email:
        create_access_session(
            CredentialCheckRequest(username=normalized_username, email=normalized_email, password=password or None, use_agent=use_agent),
            request,
            db,
        )
        return RedirectResponse(url='/gpu-monitor', status_code=303)

    if profile_email:
        create_access_session(
            CredentialCheckRequest(username=normalized_username, email=profile_email, password=password or None, use_agent=use_agent),
            request,
            db,
        )
        return RedirectResponse(url='/gpu-monitor', status_code=303)

    request.session['pending_username'] = normalized_username
    request.session['pending_password'] = password or ''
    request.session['pending_use_agent'] = use_agent
    return RedirectResponse(url='/gpu-monitor/email', status_code=303)


@app.post('/session/email')
def complete_email_step(
    request: Request,
    email: str = Form(default=''),
    db: Session = Depends(get_db),
):
    pending_username = (request.session.get('pending_username') or '').strip()
    if not pending_username:
        return RedirectResponse(url='/gpu-monitor/login', status_code=303)
    normalized_email = email.strip()
    if not normalized_email:
        return RedirectResponse(url='/gpu-monitor/email', status_code=303)

    pending_password = request.session.get('pending_password') or ''
    pending_use_agent = bool(request.session.get('pending_use_agent'))
    create_access_session(
        CredentialCheckRequest(
            username=pending_username,
            email=normalized_email,
            password=pending_password or None,
            use_agent=pending_use_agent,
        ),
        request,
        db,
    )
    request.session.pop('pending_username', None)
    request.session.pop('pending_password', None)
    request.session.pop('pending_use_agent', None)
    return RedirectResponse(url='/gpu-monitor', status_code=303)


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


@app.post('/api/notifications/test-policy-email', response_model=TestPolicyEmailResponse)
def api_test_policy_email(payload: TestPolicyEmailRequest, request: Request, db: Session = Depends(get_db)):
    viewer = (request.session.get('username') or '').strip()
    if viewer not in ADMIN_USERNAMES:
        raise HTTPException(status_code=403, detail='Only lifu and panzhou can run policy email tests.')

    username = payload.username.strip()
    profile = db.scalar(select(UserProfile).where(UserProfile.username == username))
    if not profile or not (profile.email or '').strip():
        raise HTTPException(status_code=404, detail=f'No email found in database for user "{username}".')

    host = db.scalar(select(Host).where(Host.address == payload.host_address.strip()))
    if host is None:
        raise HTTPException(status_code=404, detail=f'Host not found: "{payload.host_address}".')

    cc_email: str | None = None
    if payload.cc_lifu:
        lifu_profile = db.scalar(select(UserProfile).where(UserProfile.username == 'lifu'))
        if lifu_profile and (lifu_profile.email or '').strip():
            cc_email = lifu_profile.email.strip()

    reason = (
        f'Your 8-hour max GPU utilization is {payload.simulated_max_utilization:.2f}% '
        '(between 40% and 70%).'
    )
    subject, body = build_notification_email(host.name, host.address, username, 'avg_util_8h_40_70', reason)
    success = send_email(profile.email.strip(), subject, body, cc_email=cc_email)
    if not success:
        raise HTTPException(status_code=500, detail='Failed to send policy test email. Check SMTP settings.')
    return TestPolicyEmailResponse(
        success=True,
        username=username,
        to_email=profile.email.strip(),
        cc_email=cc_email,
        host_address=host.address,
        host_name=host.name,
        simulated_max_utilization=payload.simulated_max_utilization,
        detail='Policy-style test email sent.',
    )


@app.get('/{requested_path:path}')
def site_file_fallback(requested_path: str):
    if requested_path.startswith('api/'):
        raise HTTPException(status_code=404, detail='Not found')

    target = (SITE_ROOT / requested_path).resolve()
    if not str(target).startswith(str(SITE_ROOT.resolve())):
        raise HTTPException(status_code=404, detail='Not found')

    if target.is_dir():
        target = target / 'index.html'
    if target.exists() and target.is_file():
        return FileResponse(target)
    raise HTTPException(status_code=404, detail='Not found')
