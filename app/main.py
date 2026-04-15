from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.db import Base, SessionLocal, engine, get_db
from app.models import EmailOutbox, Host, JobKillCandidate, UserProfile
from app.schemas import (
    CredentialCheckRequest,
    EmailOutboxItem,
    EmailOutboxMarkRequest,
    EmailOutboxMarkResponse,
    HostAccessResult,
    SessionResponse,
    JobExtensionRequest,
    JobKillCandidateItem,
    JobKillResponse,
    TestEmailRequest,
    TestEmailResponse,
    TestPolicyEmailRequest,
    TestPolicyEmailResponse,
)
from app.services.analytics import get_current_status, get_gpu_history, get_user_history, get_user_storage
from app.services.collector import build_notification_email, ensure_hosts, get_collector_credentials, refresh_current_status_only, refresh_user_storage, run_collection
from app.services.notifications import send_email
from app.services.ssh_client import SshCredentials, close_collector_connections, fetch_home_users, kill_specific_gpu_processes, validate_host_access

settings = get_settings()
scheduler = BackgroundScheduler(timezone='UTC')
ADMIN_USERNAMES = {'lifu', 'panzhou'}


def _scheduled_collection() -> None:
    db = SessionLocal()
    try:
        run_collection(db)
    finally:
        db.close()


def _apply_lightweight_migrations() -> None:
    """Add columns introduced in newer versions without an Alembic pipeline."""
    insp = inspect(engine)
    if insp.has_table('daily_user_aggregates'):
        cols = {c['name'] for c in insp.get_columns('daily_user_aggregates')}
        if 'total_memory_used_mb' not in cols:
            with engine.begin() as conn:
                conn.execute(text('ALTER TABLE daily_user_aggregates ADD COLUMN total_memory_used_mb FLOAT NOT NULL DEFAULT 0'))


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    _apply_lightweight_migrations()
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


@app.get('/api/storage/users')
def api_user_storage(request: Request, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    return get_user_storage(db, allowed_hosts, viewer_username=request.session.get('username', ''))


@app.post('/api/storage/refresh')
def api_refresh_storage(request: Request, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    hosts_updated, errors = refresh_user_storage(db, allowed_hosts)
    viewer = request.session.get('username', '')
    storage = get_user_storage(db, allowed_hosts, viewer_username=viewer)
    return {'hosts_updated': hosts_updated, 'errors': errors, 'storage': storage}


def _serialize_kill_candidate(row: JobKillCandidate, host: Host, now: datetime) -> JobKillCandidateItem:
    end_at = row.killed_at or now
    total_hours = max((end_at - row.first_seen_at).total_seconds() / 3600, 0)
    return JobKillCandidateItem(
        id=row.id,
        host_name=host.name,
        host_address=host.address,
        pid=row.pid,
        username=row.username,
        gpu_index=row.gpu_index,
        utilization_gpu=row.utilization_gpu,
        memory_used_mb=row.memory_used_mb,
        status=row.status,
        kill_after=row.kill_after,
        extended_until=row.extended_until,
        extension_hours=row.extension_hours,
        extension_reason=row.extension_reason,
        first_seen_at=row.first_seen_at,
        total_running_hours=round(total_hours, 2),
    )


@app.get('/api/jobs/to-be-killed', response_model=list[JobKillCandidateItem])
def api_jobs_to_be_killed(request: Request, allowed_hosts: list[str] = Depends(get_allowed_hosts), db: Session = Depends(get_db)):
    viewer = (request.session.get('username') or '').strip()
    if not viewer or not allowed_hosts:
        return []
    stmt = (
        select(JobKillCandidate, Host)
        .join(Host, JobKillCandidate.host_id == Host.id)
        .where(
            Host.address.in_(allowed_hosts),
            JobKillCandidate.status.in_(('pending', 'extended')),
        )
        .order_by(JobKillCandidate.kill_after)
    )
    if viewer not in ADMIN_USERNAMES:
        stmt = stmt.where(JobKillCandidate.username == viewer)
    rows = db.execute(stmt).all()
    now = datetime.utcnow()
    return [_serialize_kill_candidate(row, host, now) for row, host in rows]


@app.post('/api/jobs/{job_id}/extension', response_model=JobKillResponse)
def api_request_job_extension(job_id: int, payload: JobExtensionRequest, request: Request, db: Session = Depends(get_db)):
    viewer = (request.session.get('username') or '').strip()
    if not viewer:
        raise HTTPException(status_code=401, detail='Please login first.')
    if payload.hours not in {4, 8, 12, 24}:
        raise HTTPException(status_code=400, detail='Extension must be one of 4, 8, 12, or 24 hours.')

    row = db.get(JobKillCandidate, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    if row.username != viewer:
        raise HTTPException(status_code=403, detail='You can only request extension for your own jobs.')
    if row.status not in {'pending', 'extended'}:
        raise HTTPException(status_code=400, detail='This job is not eligible for extension.')

    now = datetime.utcnow()
    row.status = 'extended'
    row.extension_hours = payload.hours
    row.extension_reason = payload.reason.strip()
    row.extension_requested_at = now
    row.extended_until = now + timedelta(hours=payload.hours)
    db.commit()
    return JobKillResponse(id=row.id, status=row.status, detail='Extension request recorded.')


@app.post('/api/jobs/{job_id}/kill', response_model=JobKillResponse)
def api_kill_job_now(job_id: int, request: Request, db: Session = Depends(get_db)):
    viewer = (request.session.get('username') or '').strip()
    if viewer not in ADMIN_USERNAMES:
        raise HTTPException(status_code=403, detail='Only lifu and panzhou can kill jobs directly.')

    row = db.get(JobKillCandidate, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Job not found.')
    host = db.get(Host, row.host_id)
    if host is None:
        raise HTTPException(status_code=404, detail='Host not found.')

    credentials = get_collector_credentials()
    if credentials is None:
        raise HTTPException(status_code=500, detail='Collector SSH credentials are not configured.')
    kill_result = kill_specific_gpu_processes(host.address, credentials, [row.pid])
    killed = {int(pid) for pid in kill_result.split(',') if pid.strip().isdigit()}
    if row.pid not in killed:
        raise HTTPException(status_code=500, detail='Failed to kill process (PID may have already exited).')

    row.status = 'killed'
    row.killed_at = datetime.utcnow()
    row.killed_by = viewer
    db.commit()
    return JobKillResponse(id=row.id, status=row.status, detail='Job killed successfully.')


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


@app.get('/api/email-outbox/pending', response_model=list[EmailOutboxItem])
def api_get_pending_emails(limit: int = 50, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(EmailOutbox)
        .where(EmailOutbox.status == 'pending')
        .order_by(EmailOutbox.created_at)
        .limit(limit)
    ).all()
    return [
        EmailOutboxItem(
            id=row.id,
            to_email=row.to_email,
            cc_email=row.cc_email,
            subject=row.subject,
            body=row.body,
            status=row.status,
            created_at=row.created_at,
        )
        for row in rows
    ]


@app.post('/api/email-outbox/{email_id}/mark-sent', response_model=EmailOutboxMarkResponse)
def api_mark_email_sent(email_id: int, db: Session = Depends(get_db)):
    row = db.get(EmailOutbox, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f'Email {email_id} not found.')
    row.status = 'sent'
    row.sent_at = datetime.utcnow()
    db.commit()
    return EmailOutboxMarkResponse(id=email_id, status='sent', detail='Marked as sent.')


@app.post('/api/email-outbox/{email_id}/mark-failed', response_model=EmailOutboxMarkResponse)
def api_mark_email_failed(email_id: int, payload: EmailOutboxMarkRequest, db: Session = Depends(get_db)):
    row = db.get(EmailOutbox, email_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f'Email {email_id} not found.')
    row.status = 'failed'
    row.error_message = payload.error_message
    db.commit()
    return EmailOutboxMarkResponse(id=email_id, status='failed', detail='Marked as failed.')
