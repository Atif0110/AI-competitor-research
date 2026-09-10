"""Generate a cryptographically strong API key for the FastAPI service."""
import secrets
print('acr_' + secrets.token_urlsafe(32))
