import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select

from fin_agent.web.auth import PASSWORDS, csrf_token, digest, new_session, verify_password
from fin_agent.web.database import Database
from fin_agent.web.models import AuthSession, ChatSession, LoginFailure, User
from fin_agent.web.settings import Settings

COOKIE = 'fin_session'


class Login(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SessionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(default='新会话', min_length=1, max_length=120)


def create_app(settings=None):
    settings = settings or Settings.from_env()
    database = Database(settings.database)
    try:
        database.check_schema()
    except Exception:
        database.close()
        raise

    @asynccontextmanager
    async def lifespan(app):
        from fin_agent.web.services import api_presence
        try:
            with api_presence(database):
                yield
        finally:
            database.close()

    app = FastAPI(title='Fin-Agent', lifespan=lifespan)
    app.state.database = database
    app.state.settings = settings
    dummy_hash = PASSWORDS.hash(secrets.token_urlsafe(32))

    @app.middleware('http')
    async def browser_boundary(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            # Includes login to prevent login CSRF; Origin is mandatory for this browser API.
            if request.headers.get('origin') != settings.origin:
                return Response(status_code=403)
        response = await call_next(request)
        if 'Cache-Control' not in response.headers:
            response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        return response

    def current_user(request: Request):
        token = request.cookies.get(COOKIE, '')
        if not token or len(token) > 128:
            raise HTTPException(401, '请重新登录')
        with database.transaction() as db:
            auth = db.get(AuthSession, digest(token))
            user = db.get(User, auth.user_id) if auth and auth.expires_at > time.time() else None
            if user is None or user.disabled:
                raise HTTPException(401, '请重新登录')
            if request.method not in {'GET', 'HEAD', 'OPTIONS'} and not secrets.compare_digest(
                    request.headers.get('x-csrf-token', ''), csrf_token(token)):
                raise HTTPException(403, '请求校验失败，请刷新页面')
            return user

    actor = Annotated[User, Depends(current_user)]
    app.state.current_user = current_user

    def admin_user(user: actor):
        if user.role != 'admin':
            raise HTTPException(403, '需要管理员权限')
        return user

    app.state.admin_user = admin_user

    @app.get('/api/v1/health')
    def health():
        with database.engine.connect() as connection:
            connection.exec_driver_sql('SELECT 1')
        return {'status': 'ok'}

    @app.post('/api/v1/auth/login')
    def login(body: Login, request: Request, response: Response):
        name, now = body.username.strip().casefold(), time.time()
        address = request.client.host if request.client else 'unknown'
        with database.transaction() as db:
            db.execute(delete(LoginFailure).where(LoginFailure.at < now - 900))
            failures = db.scalar(select(func.count()).select_from(LoginFailure).where(
                LoginFailure.address == address))
            account_failures = db.scalar(select(func.count()).select_from(LoginFailure).where(
                LoginFailure.address == address, LoginFailure.username == name))
            if failures >= 40 or account_failures >= 5:
                raise HTTPException(429, '尝试次数过多，请稍后再试', headers={'Retry-After': '900'})
            user = db.scalar(select(User).where(User.username == name))
            valid = verify_password(user.password_hash if user else dummy_hash, body.password)
            if not valid or user is None or user.disabled:
                db.add(LoginFailure(address=address, username=name, at=now))
                # Persist the failure; raising inside this transaction would roll it back.
                success = False
            else:
                success = True
                if PASSWORDS.check_needs_rehash(user.password_hash):
                    user.password_hash = PASSWORDS.hash(body.password)
                old = request.cookies.get(COOKIE)
                if old:
                    db.execute(delete(AuthSession).where(AuthSession.token_hash == digest(old)))
                db.execute(delete(AuthSession).where(AuthSession.expires_at <= now))
                db.execute(delete(LoginFailure).where(LoginFailure.address == address,
                                                      LoginFailure.username == name))
                token = new_session()
                db.add(AuthSession(token_hash=digest(token), user_id=user.id,
                                   expires_at=now + settings.session_seconds))
        if not success:
            raise HTTPException(401, '账号或密码错误')
        response.set_cookie(COOKIE, token, httponly=True, secure=settings.secure_cookie,
                            samesite='strict', path='/api', max_age=settings.session_seconds)
        return {'id': user.id, 'username': user.username, 'role': user.role, 'csrf_token': csrf_token(token)}

    @app.get('/api/v1/auth/me')
    def me(request: Request, user: actor):
        return {'id': user.id, 'username': user.username, 'role': user.role,
                'csrf_token': csrf_token(request.cookies[COOKIE])}

    @app.post('/api/v1/auth/logout', status_code=204)
    def logout(request: Request, response: Response, user: actor):
        with database.transaction() as db:
            db.execute(delete(AuthSession).where(AuthSession.token_hash == digest(request.cookies[COOKIE])))
        response.delete_cookie(COOKIE, path='/api', secure=settings.secure_cookie,
                               httponly=True, samesite='strict')

    def session_view(session):
        return {key: getattr(session, key) for key in ('id', 'title', 'created_at', 'updated_at')}

    @app.get('/api/v1/sessions')
    def list_sessions(user: actor, offset: int = 0, limit: int = 50):
        if not 0 <= offset or not 1 <= limit <= 100:
            raise HTTPException(422, '分页参数无效')
        with database.transaction() as db:
            query = select(ChatSession).where(ChatSession.user_id == user.id).order_by(
                ChatSession.updated_at.desc(), ChatSession.id).offset(offset).limit(limit)
            return {'sessions': [session_view(s) for s in db.scalars(query)]}

    @app.post('/api/v1/sessions', status_code=201)
    def add_session(body: SessionInput, user: actor):
        with database.transaction() as db:
            session = ChatSession(user_id=user.id, title=body.title)
            db.add(session)
            db.flush()
            return session_view(session)

    def owned(db, session_id, user):
        session = db.scalar(select(ChatSession).where(ChatSession.id == session_id,
                                                     ChatSession.user_id == user.id))
        if session is None:
            raise HTTPException(404, '会话不存在')
        return session

    @app.get('/api/v1/sessions/{session_id}')
    def get_session(session_id: str, user: actor):
        with database.transaction() as db:
            return session_view(owned(db, session_id, user))

    @app.patch('/api/v1/sessions/{session_id}')
    def rename_session(session_id: str, body: SessionInput, user: actor):
        with database.transaction() as db:
            session = owned(db, session_id, user)
            session.title, session.updated_at = body.title, time.time()
            return session_view(session)

    @app.delete('/api/v1/sessions/{session_id}', status_code=204)
    def remove_session(session_id: str, user: actor):
        from fin_agent.web.models import Run, DeletedSession
        from fin_agent.web.deletions import flush_deletions
        from fin_agent.web.runs import ACTIVE
        with database.transaction(immediate=True) as db:
            if db.scalar(select(Run.id).where(Run.session_id == session_id, Run.user_id == user.id,
                                             Run.status.in_(ACTIVE))):
                raise HTTPException(409, '请等待当前分析结束后再删除会话')
            db.delete(owned(db, session_id, user))
            db.add(DeletedSession(session_id=session_id, user_id=user.id))
        # A success response is sent only after the external deletion journal is durable.
        flush_deletions(database)

    from fin_agent.web.chat_routes import install_chat_routes
    install_chat_routes(app)
    from fin_agent.web.admin import install_admin_routes
    install_admin_routes(app)
    return app
