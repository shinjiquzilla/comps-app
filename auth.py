"""
Supabase Auth module - ログイン・サインアップ・セッション管理。

GoTrue (Supabase Auth) クライアントを使用。
Supabase の Project URL と anon key は Streamlit secrets で管理。
"""

import streamlit as st
from gotrue import SyncGoTrueClient, AuthResponse


def _get_auth_client() -> SyncGoTrueClient:
    """Supabase GoTrue クライアントを取得（キャッシュ）。"""
    if '_auth_client' not in st.session_state:
        url = st.secrets["supabase"]["url"]
        key = st.secrets["supabase"]["anon_key"]
        gotrue_url = f"{url}/auth/v1"
        st.session_state._auth_client = SyncGoTrueClient(
            url=gotrue_url,
            headers={"apikey": key, "Authorization": f"Bearer {key}"},
        )
    return st.session_state._auth_client


def login(email: str, password: str) -> dict:
    """
    メール+パスワードでログイン。
    Returns: {'success': bool, 'error': str|None, 'user': dict|None}
    """
    client = _get_auth_client()
    try:
        resp: AuthResponse = client.sign_in_with_password({
            "email": email,
            "password": password,
        })
        if resp.user:
            st.session_state['user'] = {
                'id': resp.user.id,
                'email': resp.user.email,
                'access_token': resp.session.access_token if resp.session else None,
            }
            st.session_state['authenticated'] = True
            return {'success': True, 'error': None, 'user': st.session_state['user']}
        return {'success': False, 'error': 'ログインに失敗しました。', 'user': None}
    except Exception as e:
        error_msg = str(e)
        if 'Invalid login' in error_msg or 'invalid' in error_msg.lower():
            error_msg = 'メールアドレスまたはパスワードが正しくありません。'
        elif 'Email not confirmed' in error_msg:
            error_msg = 'メールアドレスの確認が完了していません。確認メールをご確認ください。'
        return {'success': False, 'error': error_msg, 'user': None}


def signup(email: str, password: str) -> dict:
    """
    メール+パスワードでアカウント作成。
    Returns: {'success': bool, 'error': str|None, 'needs_confirmation': bool}
    """
    client = _get_auth_client()
    try:
        resp: AuthResponse = client.sign_up({
            "email": email,
            "password": password,
        })
        if resp.user:
            # メール確認が必要かどうか
            if resp.user.confirmed_at or (resp.session and resp.session.access_token):
                # 自動確認ON → そのままログイン
                st.session_state['user'] = {
                    'id': resp.user.id,
                    'email': resp.user.email,
                    'access_token': resp.session.access_token if resp.session else None,
                }
                st.session_state['authenticated'] = True
                return {'success': True, 'error': None, 'needs_confirmation': False}
            else:
                # メール確認待ち
                return {'success': True, 'error': None, 'needs_confirmation': True}
        return {'success': False, 'error': 'アカウント作成に失敗しました。', 'needs_confirmation': False}
    except Exception as e:
        error_msg = str(e)
        if 'already registered' in error_msg.lower() or 'already been registered' in error_msg.lower():
            error_msg = 'このメールアドレスは既に登録されています。ログインしてください。'
        elif 'password' in error_msg.lower() and ('short' in error_msg.lower() or 'least' in error_msg.lower()):
            error_msg = 'パスワードは6文字以上にしてください。'
        return {'success': False, 'error': error_msg, 'needs_confirmation': False}


def logout():
    """ログアウト。"""
    try:
        client = _get_auth_client()
        client.sign_out()
    except Exception:
        pass
    st.session_state.pop('user', None)
    st.session_state.pop('authenticated', None)
    st.session_state.pop('_auth_client', None)


def is_authenticated() -> bool:
    """ログイン済みかどうか。"""
    return st.session_state.get('authenticated', False)


def get_current_user() -> dict | None:
    """現在のログインユーザー情報。"""
    return st.session_state.get('user')


def show_login_page():
    """ログイン・サインアップページUI。"""
    st.set_page_config(
        page_title="Comps自動生成ツール - ログイン",
        page_icon="🔐",
        layout="centered",
    )

    st.markdown("## 🔐 Comps（比較会社分析）自動生成ツール")
    st.caption("くじらキャピタル株式会社")
    st.divider()

    # タブでログイン / 新規登録を切り替え
    tab_login, tab_signup = st.tabs(["ログイン", "新規アカウント作成"])

    # --- ログインタブ ---
    with tab_login:
        with st.form("login_form"):
            email = st.text_input("メールアドレス", placeholder="you@example.com", key="login_email")
            password = st.text_input("パスワード", type="password", key="login_password")
            submitted = st.form_submit_button("ログイン", use_container_width=True, type="primary")

        if submitted:
            if not email or not password:
                st.error("メールアドレスとパスワードを入力してください。")
            else:
                with st.spinner("ログイン中..."):
                    result = login(email, password)
                if result['success']:
                    st.success("ログイン成功！")
                    st.rerun()
                else:
                    st.error(result['error'])

    # --- サインアップタブ ---
    with tab_signup:
        with st.form("signup_form"):
            new_email = st.text_input("メールアドレス", placeholder="you@example.com", key="signup_email")
            new_password = st.text_input("パスワード（6文字以上）", type="password", key="signup_password")
            new_password2 = st.text_input("パスワード（確認）", type="password", key="signup_password2")
            submitted_signup = st.form_submit_button("アカウントを作成", use_container_width=True, type="primary")

        if submitted_signup:
            if not new_email or not new_password:
                st.error("メールアドレスとパスワードを入力してください。")
            elif new_password != new_password2:
                st.error("パスワードが一致しません。")
            elif len(new_password) < 6:
                st.error("パスワードは6文字以上にしてください。")
            else:
                with st.spinner("アカウント作成中..."):
                    result = signup(new_email, new_password)
                if result['success']:
                    if result['needs_confirmation']:
                        st.success("確認メールを送信しました。メール内のリンクをクリックしてからログインしてください。")
                    else:
                        st.success("アカウント作成完了！")
                        st.rerun()
                else:
                    st.error(result['error'])

    return False
