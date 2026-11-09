from typing import Any

ROLE_PERMISSIONS = {
    "employee": ["general"],
    "security_admin": ["general", "restricted"],
}


def get_allowed_access_levels(user_role: str) -> list[str]:
    """
    Return the access levels allowed for a given user role.
    """

    return ROLE_PERMISSIONS.get(user_role, [])


def filter_authorized_documents(
    documents: list[dict[str, Any]], user_role: str
) -> list[dict[str, Any]]:
    """
    Return only documents the user is authorized to access.
    """

    allowed_access_levels = get_allowed_access_levels(user_role)

    authorized_documents = []

    for document in documents:
        if document.get("access_level") in allowed_access_levels:
            authorized_documents.append(document)

    return authorized_documents
