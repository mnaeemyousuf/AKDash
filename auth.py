"""Users: password hashing, register / login / logout, profile with persona, and admin user management."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets as pysecrets
from datetime import datetime

import streamlit as st

import db
import theme

ITERATIONS = 260_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,30}$")
MIN_PASSWORD = 8

DEPARTMENTS = ["", "Management", "Sales", "Retail", "Merchandising", "Design", "Production", "Supply Chain",
               "Finance", "Marketing", "E-commerce", "HR", "IT", "Other"]
LANGUAGES = ["English", "Roman Urdu"]
DETAIL_LEVELS = ["Deep-dive (most detail)", "Balanced", "Executive summary (short)"]


class AuthError(Exception):
    """A user-facing authentication / validation message."""


# ---------- passwords ----------

def hash_password(password: str) -> str:
    salt = pysecrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"pbkdf2_sha256${ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
        return hmac.compare_digest(check, digest)
    except (ValueError, AttributeError):
        return False


def validate_password(password: str, confirm: str | None = None) -> None:
    if len(password) < MIN_PASSWORD:
        raise AuthError(f"Password must be at least {MIN_PASSWORD} characters.")
    if not (re.search(r"[A-Za-z]", password) and re.search(r"\d", password)):
        raise AuthError("Password must contain letters and numbers.")
    if confirm is not None and password != confirm:
        raise AuthError("The two passwords don't match.")


# ---------- accounts ----------

def register(username: str, password: str, confirm: str, email: str = "", full_name: str = "",
             signup_code: str | None = None, entered_code: str = "") -> int:
    username = username.strip()
    if signup_code and not hmac.compare_digest(entered_code.strip(), signup_code):
        raise AuthError("The sign-up code is not correct. Ask your administrator for it.")
    if not USERNAME_RE.match(username):
        raise AuthError("Username must be 3-30 characters: letters, numbers, dot, dash or underscore.")
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()):
        raise AuthError("Please enter a valid email address.")
    validate_password(password, confirm)
    if db.get_user_by_username(username):
        raise AuthError("That username is already taken.")
    return db.create_user(username, hash_password(password), email, full_name)


def authenticate(username: str, password: str) -> dict:
    user = db.get_user_by_username(username or "")
    if not user or not verify_password(password or "", user["password_hash"]):
        raise AuthError("Wrong username or password.")
    if not user["is_active"]:
        raise AuthError("This account is disabled. Contact your administrator.")
    db.update_user(user["id"], last_login=datetime.now().isoformat(timespec="seconds"))
    return user


def change_password(user: dict, current: str, new: str, confirm: str) -> None:
    if not verify_password(current, user["password_hash"]):
        raise AuthError("Your current password is not correct.")
    validate_password(new, confirm)
    db.update_user(user["id"], password_hash=hash_password(new))


# ---------- persona ----------

def persona_text(user: dict | None) -> str:
    """Plain-text description of the user, added to every AI prompt."""
    if not user:
        return ""
    p = user.get("persona") or {}
    lines = []
    name = user.get("full_name") or user.get("username")
    details = ", ".join(x for x in [p.get("job_title"), p.get("department"), p.get("organization")] if x)
    lines.append(f"Name: {name}" + (f" ({details})" if details else ""))
    for key, label in [("focus", "Responsibilities / decisions they make"), ("kpis", "Metrics they care about most"),
                       ("notes", "Other context")]:
        if p.get(key):
            lines.append(f"{label}: {p[key]}")
    if p.get("currency"):
        lines.append(f"Default currency/unit: {p['currency']}")
    lines.append(f"Preferred language for answers and insights: {p.get('language') or 'English'}")
    lines.append(f"Preferred level of detail: {p.get('detail') or DETAIL_LEVELS[0]}")
    return "\n".join(lines)


# ---------- session ----------

COOKIE = "akdash_session"
REMEMBER_DAYS = 7


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def remember_login(user_id: int) -> None:
    """Keep the user logged in across page reloads: random token in a cookie, its hash in the database."""
    from datetime import timedelta
    token = pysecrets.token_urlsafe(32)
    expires = (datetime.now() + timedelta(days=REMEMBER_DAYS)).isoformat(timespec="seconds")
    db.add_session(_token_hash(token), user_id, expires)
    st.session_state["_set_cookie"] = token


def write_cookie() -> None:
    """Writes (or clears) the login cookie in the browser. Called once per run, after login/logout."""
    if "_set_cookie" in st.session_state:
        token = st.session_state.pop("_set_cookie")   # our own random token (URL-safe), never user input
        js = (f"document.cookie = '{COOKIE}={token}; path=/; max-age={REMEMBER_DAYS * 86400}; SameSite=Strict';"
              if token else f"document.cookie = '{COOKIE}=; path=/; max-age=0; SameSite=Strict';")
        with st.container(height=1, border=False, key="cookie_writer"):
            st.iframe(f"<script>{js}</script>", height=1)


def current_user() -> dict | None:
    uid = st.session_state.get("user_id")
    if not uid and not st.session_state.get("_logged_out"):
        token = (st.context.cookies or {}).get(COOKIE)
        uid = db.session_user_id(_token_hash(token)) if token else None
        if uid:
            st.session_state.user_id = uid
    if not uid:
        return None
    user = db.get_user(uid)
    if not user or not user["is_active"]:
        st.session_state.pop("user_id", None)
        return None
    return user


def logout() -> None:
    token = (st.context.cookies or {}).get(COOKIE)
    if token:
        db.delete_session(_token_hash(token))   # the old cookie stops working even if the browser keeps it
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.session_state["_logged_out"] = True      # don't log back in from the (now invalid) cookie in this tab
    st.session_state["_set_cookie"] = ""        # clear the cookie in the browser
    st.rerun()


# ---------- pages ----------

def login_page(signup_code: str | None) -> None:
    _, mid, _ = st.columns([1, 1.4, 1])
    with mid:
        st.html(theme.login_header())
        first_user = not db.list_users()
        tab_in, tab_up = st.tabs(["🔐 Log in", "✨ Create account"])
        with tab_in:
            if first_user:
                st.info("No accounts yet. Create the first account: it becomes the administrator.",
                        icon=":material/info:")
            with st.form("login_form", border=False):
                username = st.text_input("Username", autocomplete="username")
                password = st.text_input("Password", type="password", autocomplete="current-password")
                remember = st.checkbox(f"Keep me logged in for {REMEMBER_DAYS} days", value=True)
                if st.form_submit_button("Log in", type="primary", width="stretch", icon=":material/login:"):
                    try:
                        user = authenticate(username, password)
                        st.session_state.pop("_logged_out", None)
                        st.session_state.user_id = user["id"]
                        if remember:
                            remember_login(user["id"])
                        st.rerun()
                    except AuthError as e:
                        st.error(str(e))
        with tab_up:
            with st.form("register_form", border=False):
                c1, c2 = st.columns(2)
                full_name = c1.text_input("Full name")
                username = c2.text_input("Username", help="3-30 characters: letters, numbers, . _ -")
                email = st.text_input("Email (optional)")
                c1, c2 = st.columns(2)
                password = c1.text_input("Password", type="password", autocomplete="new-password",
                                         help=f"At least {MIN_PASSWORD} characters, with letters and numbers.")
                confirm = c2.text_input("Confirm password", type="password", autocomplete="new-password")
                code = st.text_input("Sign-up code", type="password") if signup_code and not first_user else ""
                if st.form_submit_button("Create account", type="primary", width="stretch",
                                         icon=":material/person_add:"):
                    try:
                        uid = register(username, password, confirm, email, full_name,
                                       None if first_user else signup_code, code)
                        st.session_state.pop("_logged_out", None)
                        st.session_state.user_id = uid
                        remember_login(uid)
                        st.session_state.flash = ("Welcome to AKDash! Set up your persona in My profile so "
                                                  "the AI knows who you are.", [])
                        st.rerun()
                    except AuthError as e:
                        st.error(str(e))
        st.caption("AKDash · AI dashboards from your Excel, CSV and PDF reports")


def profile_page(user: dict) -> None:
    theme.hero(user.get("full_name") or user["username"], "My profile & persona",
               [f"👤 @{user['username']}", f"🛡️ {user['role'].title()}",
                f"📅 Member since {user['created_at'][:10]}"])
    p = dict(user.get("persona") or {})
    tab_persona, tab_account, tab_pw = st.tabs(["🧑‍💼 Persona", "🪪 Account", "🔑 Password"])

    with tab_persona, st.container(border=True, key="card_persona"):
        st.html('<p class="ak-card-title">Who are you?</p><p class="ak-card-sub">This is sent to the AI with every '
                'request, so dashboards, answers and insights are written for you.</p>')
        with st.form("persona_form", border=False):
            c1, c2 = st.columns(2)
            job_title = c1.text_input("Job title", p.get("job_title", ""), placeholder="e.g. Regional Sales Manager")
            dept_idx = DEPARTMENTS.index(p.get("department", "")) if p.get("department", "") in DEPARTMENTS else 0
            department = c2.selectbox("Department", DEPARTMENTS, index=dept_idx,
                                      format_func=lambda d: d or "— choose —")
            c1, c2 = st.columns(2)
            organization = c1.text_input("Organization", p.get("organization", ""), placeholder="e.g. Alkaram Textiles")
            currency = c2.text_input("Default currency / unit", p.get("currency", ""), placeholder="e.g. Rs")
            focus = st.text_area("Your responsibilities & the decisions you make with data", p.get("focus", ""),
                                 height=90, placeholder="e.g. I manage 40 retail outlets in Punjab and decide stock "
                                                        "transfers and weekly sales targets.")
            kpis = st.text_area("Metrics you care about most", p.get("kpis", ""), height=70,
                                placeholder="e.g. Sell-through %, sales per store, stock ageing, returns")
            c1, c2 = st.columns(2)
            language = c1.selectbox("Language for AI answers", LANGUAGES,
                                    index=LANGUAGES.index(p.get("language", "English"))
                                    if p.get("language") in LANGUAGES else 0)
            detail = c2.selectbox("Level of detail", DETAIL_LEVELS,
                                  index=DETAIL_LEVELS.index(p["detail"]) if p.get("detail") in DETAIL_LEVELS else 0)
            notes = st.text_area("Anything else the AI should know", p.get("notes", ""), height=70)
            if st.form_submit_button("Save persona", type="primary", icon=":material/save:"):
                db.update_user(user["id"], persona={
                    "job_title": job_title.strip(), "department": department, "organization": organization.strip(),
                    "currency": currency.strip(), "focus": focus.strip(), "kpis": kpis.strip(),
                    "language": language, "detail": detail, "notes": notes.strip()})
                st.toast("Persona saved. The AI will use it from your next request.", icon="✅")
                st.rerun()
        with st.expander("Preview: what the AI is told about you"):
            st.code(persona_text(user), language=None)

    with tab_account, st.container(border=True, key="card_account"):
        with st.form("account_form", border=False):
            full_name = st.text_input("Full name", user.get("full_name", ""))
            email = st.text_input("Email", user.get("email", ""))
            st.text_input("Username", user["username"], disabled=True)
            if st.form_submit_button("Save account", type="primary", icon=":material/save:"):
                if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()):
                    st.error("Please enter a valid email address.")
                else:
                    db.update_user(user["id"], full_name=full_name.strip(), email=email.strip())
                    st.toast("Account updated.", icon="✅")
                    st.rerun()

    with tab_pw, st.container(border=True, key="card_password"):
        with st.form("password_form", border=False, clear_on_submit=True):
            current = st.text_input("Current password", type="password", autocomplete="current-password")
            new = st.text_input("New password", type="password", autocomplete="new-password")
            confirm = st.text_input("Confirm new password", type="password", autocomplete="new-password")
            if st.form_submit_button("Change password", type="primary", icon=":material/key:"):
                try:
                    change_password(user, current, new, confirm)
                    db.delete_user_sessions(user["id"])   # log out other devices
                    remember_login(user["id"])
                    st.toast("Password changed. Other devices were logged out.", icon="✅")
                except AuthError as e:
                    st.error(str(e))


def admin_page(user: dict) -> None:
    theme.hero("User management", "Activate, deactivate, promote and reset users", ["🛡️ Admins only"])
    users = db.list_users()
    rows = [{"ID": u["id"], "Username": u["username"], "Name": u["full_name"], "Email": u["email"],
             "Role": u["role"], "Active": bool(u["is_active"]), "Projects": len(db.list_projects(u["id"])),
             "Last login": (u["last_login"] or "—")[:16].replace("T", " ")} for u in users]
    with st.container(border=True, key="card_users"):
        st.dataframe(rows, hide_index=True, width="stretch")
    with st.container(border=True, key="card_user_edit"):
        st.html('<p class="ak-card-title">Edit a user</p>')
        labels = {u["id"]: f"{u['username']} ({u['full_name'] or 'no name'})" for u in users}
        uid = st.selectbox("User", list(labels), format_func=labels.get)
        target = db.get_user(uid)
        is_self = uid == user["id"]
        c1, c2 = st.columns(2)
        role = c1.selectbox("Role", ["user", "admin"], index=["user", "admin"].index(target["role"]), disabled=is_self)
        active = c2.toggle("Active", bool(target["is_active"]), disabled=is_self)
        if st.button("Save changes", type="primary", icon=":material/save:", disabled=is_self):
            demoting = target["role"] == "admin" and (role != "admin" or not active)
            if demoting and db.count_admins() <= 1:
                st.error("There must be at least one active admin.")
            else:
                db.update_user(uid, role=role, is_active=int(active))
                if not active:
                    db.delete_user_sessions(uid)
                st.toast("User updated.", icon="✅")
                st.rerun()
        if is_self:
            st.caption("You can't change your own role or status here.")
        with st.expander("Reset password"):
            temp = st.text_input("New temporary password", type="password", key=f"reset_{uid}")
            if st.button("Reset password", icon=":material/lock_reset:"):
                try:
                    validate_password(temp)
                    db.update_user(uid, password_hash=hash_password(temp))
                    db.delete_user_sessions(uid)
                    st.toast(f"Password reset for {target['username']}. Share it with them securely.", icon="✅")
                except AuthError as e:
                    st.error(str(e))
