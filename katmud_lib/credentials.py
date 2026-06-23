"""katmud_lib.credentials - Windows Credential Manager via keyring.

Key: katmud/<mud>/<character>  (spec rename note supersedes the older
"pymud/" wording in section 3).

No password ever touches a file in the project directory - the whole
tree is publish-safe by construction.

Threat model (stated honestly, spec section 3): protects against file
sharing / backup / sync exposure and against other user accounts on
the same machine. Does NOT protect against malware already running as
the current user. Appropriate for MUD credentials.

keyring is the only third-party dependency:  pip install keyring
If it's absent everything degrades to "no stored password" - the
client will prompt every connect and explain how to install.
"""

from . import paths

try:
    import keyring
    try:
        # fail fast on broken backends (e.g. headless Linux without
        # SecretService) instead of erroring at first get/set
        from keyring.errors import KeyringError
    except ImportError:          # very old keyring
        class KeyringError(Exception):
            pass
except ImportError:
    keyring = None

    class KeyringError(Exception):
        pass

UNAVAILABLE_MSG = ("keyring module not installed - passwords cannot be "
                   "stored. Install with:  pip install keyring")


def _key(mud, character):
    return f"{paths.KEYRING_SERVICE}/{paths.safe_name(mud)}/" \
           f"{paths.safe_name(character)}"


def available():
    return keyring is not None


def get_password(mud, character):
    """Stored password or None (None also when keyring is absent)."""
    if keyring is None:
        return None
    try:
        return keyring.get_password(_key(mud, character), character)
    except KeyringError:
        return None


def set_password(mud, character, password):
    """Store/overwrite. Returns error string or None."""
    if keyring is None:
        return UNAVAILABLE_MSG
    try:
        keyring.set_password(_key(mud, character), character, password)
        return None
    except KeyringError as e:
        return f"credential store failed: {e}"


def delete_password(mud, character):
    if keyring is None:
        return UNAVAILABLE_MSG
    try:
        keyring.delete_password(_key(mud, character), character)
        return None
    except KeyringError as e:
        return f"credential delete failed: {e}"
    except Exception:
        # keyring raises PasswordDeleteError (a KeyringError in modern
        # versions) when no entry exists; treat as success.
        return None


def key_label(mud, character):
    """For user-facing messages naming the key explicitly."""
    return _key(mud, character)
