#!/usr/bin/env python3
"""
scripts/dropbox_auth.py — One-time Dropbox OAuth setup.

Run this LOCALLY (on your laptop, not the server) to generate the
DROPBOX_REFRESH_TOKEN you need in your server's .env file.

Why:
    Dropbox deprecated long-lived access tokens in September 2021.
    All new Dropbox apps must use short-lived tokens obtained via OAuth 2.0
    with a refresh token. The refresh token never expires as long as you
    keep using it — it refreshes automatically via the dropbox SDK.

Prerequisites:
    1. Create a Dropbox app at https://www.dropbox.com/developers/apps
       - Choose "Scoped access"
       - Choose "Full Dropbox" access
       - Name it anything (e.g. "ragbase-backup")
    2. In the app settings, under "Permissions", enable:
       - files.content.write
       - files.content.read
    3. Under "Settings", set "OAuth 2 / Access token expiration"
       to "Short-lived" (this is the new default)
    4. Note your App key and App secret

Usage:
    pip install dropbox
    python scripts/dropbox_auth.py

The script will:
    1. Ask for your App key and App secret
    2. Print an authorization URL — open it in your browser
    3. After you authorize, Dropbox redirects to a localhost URL
       that will fail to load (that's expected) — copy the `code=`
       parameter from that URL
    4. Paste the code back into the terminal
    5. Print your DROPBOX_REFRESH_TOKEN — copy it to your server's .env
"""
import sys


def main():
    try:
        import dropbox
        from dropbox import DropboxOAuth2FlowNoRedirect
    except ImportError:
        print("ERROR: dropbox package not installed.")
        print("Run: pip install dropbox")
        sys.exit(1)

    print("=" * 60)
    print("  RAGBase — Dropbox OAuth Setup")
    print("=" * 60)
    print()
    print("You need your App key and App secret from:")
    print("https://www.dropbox.com/developers/apps")
    print()

    app_key    = input("App key:    ").strip()
    app_secret = input("App secret: ").strip()

    if not app_key or not app_secret:
        print("ERROR: App key and secret are required.")
        sys.exit(1)

    # Offline scope = gets a refresh token (not just short-lived access token)
    auth_flow = DropboxOAuth2FlowNoRedirect(
        app_key,
        app_secret,
        token_access_type="offline",
    )

    authorize_url = auth_flow.start()

    print()
    print("-" * 60)
    print("Step 1: Open this URL in your browser:")
    print()
    print(f"  {authorize_url}")
    print()
    print("Step 2: Click 'Allow' to authorize RAGBase.")
    print()
    print("Step 3: You will be redirected to a URL that fails to load.")
    print("        That is expected. Copy the value after 'code=' in the URL.")
    print("        Example: http://localhost/?code=ABC123&...")
    print("                                              ^^^^^^ copy this")
    print("-" * 60)
    print()

    auth_code = input("Paste the authorization code here: ").strip()

    if not auth_code:
        print("ERROR: No code entered.")
        sys.exit(1)

    try:
        oauth_result = auth_flow.finish(auth_code)
    except Exception as e:
        print(f"\nERROR: Authorization failed: {e}")
        print("Make sure you copied the full code from the URL.")
        sys.exit(1)

    refresh_token = oauth_result.refresh_token
    account_id    = oauth_result.account_id

    if not refresh_token:
        print("\nERROR: No refresh token returned.")
        print(
            "Make sure your app has 'offline' access type enabled "
            "and 'Short-lived' token expiration is set."
        )
        sys.exit(1)

    # Verify the token works
    try:
        dbx  = dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
        )
        acct = dbx.users_get_current_account()
        print(f"\n✓ Authentication successful — connected as: {acct.email}")
    except Exception as e:
        print(f"\nWARNING: Could not verify token: {e}")
        print("The token may still be valid — proceed with caution.")

    print()
    print("=" * 60)
    print("  Add these three lines to your server's .env file:")
    print("=" * 60)
    print()
    print(f"DROPBOX_APP_KEY={app_key}")
    print(f"DROPBOX_APP_SECRET={app_secret}")
    print(f"DROPBOX_REFRESH_TOKEN={refresh_token}")
    print()
    print("Account ID (for reference):", account_id)
    print()
    print(
        "NOTE: Never commit .env to git. "
        "The refresh token gives full Dropbox access."
    )
    print("=" * 60)


if __name__ == "__main__":
    main()