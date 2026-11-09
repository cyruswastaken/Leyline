"""Knowledge-base administration (role: kb_admin).

Administrators manage documents and who may read them. They do *not* gain
read access to content through this role: there is no endpoint that returns
document text, only metadata, status and ACLs.
"""

from __future__ import annotations

import dataclasses
import uuid
from datetime import datetime
from typing import Any

from flask import Blueprint, Response, jsonify, render_template, request
from flask.typing import ResponseReturnValue

from leyline.auth.access import current_principal, require_principal, require_roles
from leyline.auth.principal import AppRole
from leyline.extensions import app_settings, ingestion_service
from leyline.ingestion.parsers import InvalidDocumentError
from leyline.ingestion.service import (
    DocumentNotFoundError,
    DocumentStateError,
)

bp = Blueprint("admin", __name__)

# Multipart framing overhead allowed on top of the file size limit.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)
        }
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _error(message: str, status: int) -> ResponseReturnValue:
    return jsonify({"error": message}), status


@bp.errorhandler(InvalidDocumentError)
def _invalid(exc: InvalidDocumentError) -> ResponseReturnValue:
    return _error(str(exc), 400)


@bp.errorhandler(DocumentNotFoundError)
def _not_found(_: DocumentNotFoundError) -> ResponseReturnValue:
    return _error("Document not found", 404)


@bp.errorhandler(DocumentStateError)
def _conflict(exc: DocumentStateError) -> ResponseReturnValue:
    return _error(str(exc), 409)


@bp.get("/admin/documents")
@require_roles(AppRole.KB_ADMIN)
def page() -> ResponseReturnValue:
    return render_template("admin_documents.html", user=current_principal())


@bp.get("/api/admin/documents")
@require_roles(AppRole.KB_ADMIN)
def list_documents() -> ResponseReturnValue:
    documents = ingestion_service().list_documents()
    return jsonify({"documents": _jsonable(documents)})


@bp.get("/api/admin/documents/<uuid:document_id>")
@require_roles(AppRole.KB_ADMIN)
def get_document(document_id: uuid.UUID) -> ResponseReturnValue:
    return jsonify(_jsonable(ingestion_service().get_document(document_id)))


def _read_upload() -> tuple[str, bytes]:
    """The uploaded file's name and bytes, bounded by the upload limit."""
    limit = app_settings().max_upload_bytes
    # The global request limit is tiny (JSON API); allow this route more.
    request.max_content_length = limit + _MULTIPART_OVERHEAD_BYTES

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise InvalidDocumentError("Choose a file to upload.")
    data = upload.read(limit + 1)
    if len(data) > limit:
        raise InvalidDocumentError(
            f"The file is larger than the {limit // (1024 * 1024)} MB limit."
        )
    return upload.filename, data


def _submit_response(result: Any) -> ResponseReturnValue:
    body = {
        "document_id": str(result.document_id),
        "version_id": str(result.version_id),
        "version": result.version,
        "duplicate": result.duplicate,
    }
    return jsonify(body), 200 if result.duplicate else 202


@bp.post("/api/admin/documents")
@require_roles(AppRole.KB_ADMIN)
def upload_document() -> ResponseReturnValue:
    principal = require_principal()
    filename, data = _read_upload()
    groups = [g for item in request.form.getlist("groups") for g in item.split(",")]
    result = ingestion_service().submit_upload(
        actor_id=principal.user_id,
        filename=filename,
        data=data,
        title=request.form.get("title", ""),
        groups=groups,
    )
    return _submit_response(result)


@bp.post("/api/admin/documents/<uuid:document_id>/versions")
@require_roles(AppRole.KB_ADMIN)
def upload_version(document_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    filename, data = _read_upload()
    result = ingestion_service().submit_upload(
        actor_id=principal.user_id,
        filename=filename,
        data=data,
        title=request.form.get("title") or None,
        document_id=document_id,
    )
    return _submit_response(result)


@bp.put("/api/admin/documents/<uuid:document_id>/acl")
@require_roles(AppRole.KB_ADMIN)
def set_acl(document_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    body = request.get_json(silent=True)
    groups = body.get("groups") if isinstance(body, dict) else None
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        return _error('Expected JSON like {"groups": ["staff"]}', 400)
    acl = ingestion_service().set_acl(
        actor_id=principal.user_id, document_id=document_id, groups=groups
    )
    return jsonify({"groups": sorted(acl)})


@bp.post("/api/admin/documents/<uuid:document_id>/reindex")
@require_roles(AppRole.KB_ADMIN)
def reindex(document_id: uuid.UUID) -> ResponseReturnValue:
    principal = require_principal()
    queued = ingestion_service().reindex(
        actor_id=principal.user_id, document_id=document_id
    )
    return jsonify({"queued": queued}), 202 if queued else 200


@bp.delete("/api/admin/documents/<uuid:document_id>")
@require_roles(AppRole.KB_ADMIN)
def delete_document(document_id: uuid.UUID) -> Response | ResponseReturnValue:
    principal = require_principal()
    ingestion_service().delete_document(
        actor_id=principal.user_id, document_id=document_id
    )
    return "", 204
