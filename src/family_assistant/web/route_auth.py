"""Single source of truth for `/api` route authentication classification.

Every `/api/*` request is authenticated by default (session or API token) in
:class:`~family_assistant.web.auth.AuthMiddleware`. The paths below are the
complete set of exceptions, each authenticated by something else or public by
design:

- ``bootstrap``: endpoints whose purpose is obtaining a credential; they carry
  their own authentication (PKCE code + verifier, opaque token, session).
- ``public``: deliberately unauthenticated receivers with their own abuse
  controls.
- ``scoped``: routes whose access control lives outside default auth — either
  a route dependency granting narrower access than default auth (diagnostics
  readonly token) or a custom transport-level credential (the Asterisk
  WebSocket's query-token handshake, which cannot carry headers/cookies).

Separately, :data:`DIAGNOSTICS_TOKEN_ROUTES` are routes the edge still guards:
it requires a JWT, unless the request carries the diagnostics token in
:data:`DIAGNOSTICS_TOKEN_HEADER`, which the gateway verifies itself. Default
auth defers to the route's ``get_diagnostics_reader`` dependency for them too.

The same classification drives the edge deployment's JWT-enforcement route
split (see docs/design/jwt-edge-auth.md): it is published at
``/.well-known/auth-route-classification`` so the gateway configuration can be
generated or contract-tested against it rather than hand-mirrored.
"""

import re
from typing import Literal, TypedDict

RouteClass = Literal["bootstrap", "public", "scoped"]
PUBLIC_ERROR_INTAKE_METHOD = "POST"
PUBLIC_ERROR_INTAKE_PATH = "/api/errors/"

# Functional TypedDict syntax: "class" is a reserved word in class syntax but
# is the published JSON contract's key.
RouteExemption = TypedDict(
    "RouteExemption",
    {"match": str, "path": str, "methods": list[str], "class": str},
)


class EdgeKeyedRoute(TypedDict):
    """A route the edge admits with a gateway-verified key header instead of a JWT."""

    match: str
    path: str
    methods: list[str]
    header: str


class RouteClassificationDocument(TypedDict):
    """The document served at /.well-known/auth-route-classification."""

    jwt_required_prefixes: list[str]
    no_jwt_routes: list["RouteExemption"]
    diagnostics_token_routes: list[EdgeKeyedRoute]


# (methods, match, path, class). "exact" matches only that path; "prefix"
# matches the path itself and anything beneath it; "regex" is full-matched.
NO_DEFAULT_AUTH_ROUTES: list[tuple[frozenset[str], str, str, RouteClass]] = [
    (frozenset({"POST"}), "exact", "/api/auth/exchange", "bootstrap"),
    (frozenset({"POST"}), "exact", "/api/auth/refresh", "bootstrap"),
    (frozenset({"POST"}), "exact", "/api/auth/token", "bootstrap"),
    (frozenset({"GET"}), "exact", "/api/auth/browser-token", "bootstrap"),
    # OAuth return target: the browser can come back from the provider's
    # consent page after arbitrarily long (the JWT cookie may have expired
    # while away), so the gateway must let it through to the backend's own
    # state + session validation.
    (
        frozenset({"GET"}),
        "exact",
        "/api/integrations/google/callback",
        "bootstrap",
    ),
    # Custom-auth transports: the Asterisk WebSocket authenticates with its
    # own ?token= secret during the handshake and cannot present a JWT or
    # browser cookie. Any future non-HTTP-auth transport joins this class.
    (frozenset({"GET"}), "exact", "/api/asterisk/live", "scoped"),
    (
        frozenset({PUBLIC_ERROR_INTAKE_METHOD}),
        "exact",
        PUBLIC_ERROR_INTAKE_PATH,
        "public",
    ),
    # Scoped: diagnostics readonly token checked in get_diagnostics_reader.
    # Narrow to exactly the routes that accept it; sibling debug routes use
    # default authentication.
    (frozenset({"GET"}), "exact", "/api/debug/profiles/tools", "scoped"),
    (frozenset({"GET"}), "prefix", "/api/diagnostics", "scoped"),
    (
        frozenset({"GET"}),
        "regex",
        r"^/api/errors/(telemetry|[0-9]+)?$",
        "scoped",
    ),
]


# The header carrying the diagnostics readonly token on DIAGNOSTICS_TOKEN_ROUTES.
# A dedicated header rather than Authorization: the gateway selects the
# key-verified route by its presence, so it must not collide with the JWT a
# browser or native client sends to the same paths.
DIAGNOSTICS_TOKEN_HEADER = "X-Diagnostics-Token"

# (methods, match, path). Shared-conversation reads, so a coding agent holding
# the diagnostics token can pull a conversation whose share link it was given.
# Both secrets are needed: the share token selects the conversation, and the
# diagnostics token is checked at the edge before the backend sees the request.
DIAGNOSTICS_TOKEN_ROUTES: list[tuple[frozenset[str], str, str]] = [
    (
        frozenset({"GET"}),
        "regex",
        r"^/api/v1/shared-conversations/[^/]+/(messages|attachments/[^/]+)$",
    ),
]


def _route_matches(
    method: str,
    path: str,
    methods: frozenset[str],
    match: str,
    route_path: str,
) -> bool:
    if method.upper() not in methods:
        return False
    if match == "exact":
        return path == route_path
    if match == "prefix":
        prefix = route_path.rstrip("/")
        return path == prefix or path.startswith(f"{prefix}/")
    if match == "regex":
        return re.fullmatch(route_path, path) is not None
    raise ValueError(f"Unknown route match type: {match}")


def is_api_path(path: str) -> bool:
    """Whether a request path is under the API prefix."""
    return path == "/api" or path.startswith("/api/")


def is_embedded_app_path(path: str) -> bool:
    """Whether a request belongs to the JWT-authenticated embedded app."""
    return path == "/app" or path.startswith("/app/")


def is_token_authenticated_path(path: str) -> bool:
    """API and embedded pages share credential validation, not route exemptions."""
    return is_api_path(path) or is_embedded_app_path(path)


def api_route_requires_default_auth(method: str, path: str) -> bool:
    """Whether an /api request must pass default middleware authentication.

    True for everything under /api that is not in NO_DEFAULT_AUTH_ROUTES —
    fail-closed: an unmatched or misclassified path still requires auth.
    """
    if not is_api_path(path):
        return False
    routes = [
        (methods, match, route_path)
        for methods, match, route_path, _ in NO_DEFAULT_AUTH_ROUTES
    ] + DIAGNOSTICS_TOKEN_ROUTES
    return not any(
        _route_matches(method, path, methods, match, route_path)
        for methods, match, route_path in routes
    )


def is_public_error_intake(method: str, path: str) -> bool:
    """Whether this request is the deliberately public error receiver."""
    return (
        method.upper() == PUBLIC_ERROR_INTAKE_METHOD
        and path == PUBLIC_ERROR_INTAKE_PATH
    )


def api_route_classification() -> RouteClassificationDocument:
    """The published classification document for edge policy generation."""
    return {
        "jwt_required_prefixes": ["/api/", "/app/"],
        "no_jwt_routes": [
            {
                "match": match,
                "path": route_path,
                "methods": sorted(methods),
                "class": route_class,
            }
            for methods, match, route_path, route_class in NO_DEFAULT_AUTH_ROUTES
        ],
        "diagnostics_token_routes": [
            {
                "match": match,
                "path": route_path,
                "methods": sorted(methods),
                "header": DIAGNOSTICS_TOKEN_HEADER,
            }
            for methods, match, route_path in DIAGNOSTICS_TOKEN_ROUTES
        ],
    }


BOOTSTRAP_BODY_LIMIT_BYTES = 64 * 1024


# The MCP adapter's OAuth 2.1 protocol endpoints (docs/design/mcp-adapter.md).
# They sit outside /api and authenticate themselves (client credentials, PKCE,
# or nothing at all for dynamic registration), so like the bootstrap routes
# their bodies are capped before the SDK buffers and parses them.
OAUTH_PROTOCOL_PATHS: frozenset[str] = frozenset({
    "/authorize",
    "/token",
    "/register",
    "/revoke",
})
# The consent page's form POST. It is authenticated, not public, but FastAPI
# buffers a form body before its dependencies run, so the cap applies here too.
MCP_CONSENT_PATH = "/mcp/consent"
BODY_CAPPED_PAGE_PATHS: frozenset[str] = OAUTH_PROTOCOL_PATHS | {MCP_CONSENT_PATH}


def api_route_body_limit(method: str, path: str) -> int | None:
    """Byte cap for a request body on this route; None means unlimited.

    The no-default-auth routes are reachable unauthenticated (bootstrap and
    public receivers), so their bodies are capped before buffering, as are the
    OAuth protocol endpoints and the consent form. Default-auth routes are
    excluded — they legitimately carry large payloads such as attachment
    uploads.
    """
    if method.upper() not in {"POST", "PUT", "PATCH"}:
        return None
    if path in BODY_CAPPED_PAGE_PATHS:
        return BOOTSTRAP_BODY_LIMIT_BYTES
    if not is_api_path(path) or api_route_requires_default_auth(method, path):
        return None
    return BOOTSTRAP_BODY_LIMIT_BYTES
