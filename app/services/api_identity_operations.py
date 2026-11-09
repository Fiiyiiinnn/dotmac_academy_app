"""Fixed API commands; guard projections never supply writer proof."""

# Ported at new-composition genesis (presentation-coupled wave).

from app.services import audit_context
from app.services.identity import normalize_email


def create_person(db, *, request, email, first_name, last_name):
    context = None
    try:
        with audit_context.create_api_person(
            db, request=request, email=normalize_email(email), first_name=first_name, last_name=last_name
        ) as context:
            return context.create_person()
    finally:
        db = request = email = first_name = last_name = context = None


def delete_person(db, *, request, person_id):
    context = None
    try:
        with audit_context.delete_api_person(db, request=request, person_id=person_id) as context:
            context.delete_person()
    finally:
        db = request = person_id = context = None


def create_role(db, *, request, slug, name):
    context = None
    try:
        with audit_context.create_api_role(db, request=request, slug=slug, name=name) as context:
            return context.create_role()
    finally:
        db = request = slug = name = context = None


def grant_role(db, *, request, person_id, role_id):
    context = None
    try:
        with audit_context.grant_api_role(db, request=request, person_id=person_id, role_id=role_id) as context:
            context.grant_role()
    finally:
        db = request = person_id = role_id = context = None
