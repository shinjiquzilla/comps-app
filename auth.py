"""
Supabase Auth module - ログイン・セッション管理。

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
            error_msg = 'メールアドレスの確認が完了していません。招待メールをご確認ください。'
        return {'success': False, 'error': error_msg, 'user': None}


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
    """ログインページUI。認証成功時に True を返す。"""
    st.set_page_config(
        page_title="Comps自動生成ツール - ログイン",
        page_icon="🔐",
        layout="centered",
    )

    st.markdown("## 🔐 ログイン")
    st.markdown("**Comps（比較会社分析）自動生成ツール**")
    st.caption("くじらキャピタル株式会社")
    st.divider()

    with st.form("login_form"):
        email = st.text_input("メールアドレス", placeholder="you@example.com")
        password = st.text_input("パスワード", type="password")
        submitted = st.form_submit_button("ログイン", use_container_width=True, type="primary")

    if submitted:
        if not email or not password:
            st.error("メールアドレスとパスワードを入力してください。")
            return False

        with st.spinner("ログイン中..."):
            result = login(email, password)

        if result['success']:
            st.success("ログイン成功！")
            st.rerun()
            return True
        else:
            st.error(result['error'])
            return False

    st.divider()
    st.caption("アカウントをお持ちでない場合は、くじらキャピタルの担当者にお問い合わせください。")
    return False
