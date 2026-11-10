from flask import Blueprint, jsonify, render_template, request
from flask.typing import ResponseReturnValue

from leyline.auth.access import require_roles
from leyline.auth.principal import AppRole
from leyline.extensions import database
from leyline.services.monitoring import (
    DEFAULT_RECENT,
    get_recent_events,
    get_security_summary,
)

bp = Blueprint("monitoring", __name__)

# Security events reveal who did what, so only auditors may read them.


@bp.get("/monitoring/summary")
@require_roles(AppRole.SECURITY_AUDITOR)
def summary() -> ResponseReturnValue:
    return jsonify(get_security_summary(database())), 200


@bp.get("/monitoring/events")
@require_roles(AppRole.SECURITY_AUDITOR)
def events() -> ResponseReturnValue:
    limit = request.args.get("limit", DEFAULT_RECENT, type=int)
    event_type = request.args.get("type") or None
    return jsonify(get_recent_events(database(), limit, event_type=event_type)), 200


@bp.get("/monitoring")
@require_roles(AppRole.SECURITY_AUDITOR)
def dashboard() -> ResponseReturnValue:
    return render_template("monitoring.html")
