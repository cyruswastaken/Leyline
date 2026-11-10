"""Pages anyone may open: the landing page. Nothing here reads user data."""

from flask import Blueprint, redirect, render_template
from flask.typing import ResponseReturnValue

from leyline.auth.access import current_principal

bp = Blueprint("public", __name__)


@bp.get("/welcome")
def welcome() -> ResponseReturnValue:
    if current_principal() is not None:
        return redirect("/")
    return render_template("landing.html")
