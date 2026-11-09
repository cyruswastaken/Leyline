from flask import Blueprint, jsonify, render_template
from flask.typing import ResponseReturnValue

from leyline.services.monitoring import (
    get_recent_events,
    get_security_summary,
)

bp = Blueprint("monitoring", __name__)

# TODO(Phase 1): these routes expose operational data and must be
# restricted to an authenticated security_auditor before any shared use.


@bp.get("/monitoring/summary")
def summary() -> ResponseReturnValue:
    return jsonify(get_security_summary()), 200


@bp.get("/monitoring/events")
def events() -> ResponseReturnValue:
    return jsonify(get_recent_events()), 200


@bp.get("/monitoring")
def dashboard() -> ResponseReturnValue:
    return render_template("monitoring.html")
