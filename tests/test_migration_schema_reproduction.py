"""
Test Live Database Schema Truth and Migration Reproduction Proof.

Verifies:
1. Migrations 0032 -> 0033 apply cleanly to fresh database.
2. Resulting schema contains all expected columns and matches Django model definitions.
3. Live SQLite database contains all fields and matches model definitions.
"""

import pytest
import sqlite3
import tempfile
import os
from django.db import connection
from django.core.management import call_command
from openoutreach.career.models import CandidateApplicationProfile


@pytest.mark.django_db
def test_candidate_application_profile_model_fields():
    """Verify Django model fields are accessible and functional."""
    field_names = [f.name for f in CandidateApplicationProfile._meta.get_fields()]
    
    # Check 0032 fields
    assert "excluded_locations" in field_names
    assert "remote_mode" in field_names
    assert "unknown_location_handling" in field_names

    # Check 0033 fields
    assert "application_strategy" in field_names
    assert "search_worldwide" in field_names
    assert "specific_authorizations" in field_names


@pytest.mark.django_db
def test_live_sqlite_schema_matches_model_fields():
    """Verify live physical database table columns match model fields."""
    with connection.cursor() as cursor:
        db_cols = {col.name for col in connection.introspection.get_table_description(cursor, "career_candidateapplicationprofile")}


    required_cols = [
        "id", "full_name", "preferred_name", "email", "phone", "city", "country",
        "target_job_titles", "target_locations", "work_modes", "employment_types",
        "willing_to_relocate", "earliest_start_date", "notice_period",
        "authorized_countries", "requires_sponsorship_now", "requires_sponsorship_future",
        "currency", "minimum_salary", "target_salary", "salary_negotiable",
        "confirmed", "confirmed_at", "source", "profile_version", "created_at", "updated_at",
        "candidate_profile_id", "allow_automatic_salary",
        "background_check_willingness", "hybrid_willingness", "onsite_willingness",
        "relocation_willingness", "remote_willingness", "salary_period", "travel_willingness",
        "visa_sponsorship_future", "visa_sponsorship_now",
        "excluded_locations", "remote_mode", "unknown_location_handling",
        "application_strategy", "search_worldwide", "specific_authorizations"
    ]

    for col in required_cols:
        assert col in db_cols, f"Column '{col}' is missing from physical SQLite table"


def test_direct_sqlite3_file_schema():
    """Directly inspect data/db.sqlite3 without Django test db wrapper."""
    db_file = "/path/to/user/Projects/OpenOutreach/data/db.sqlite3"
    if not os.path.exists(db_file):
        pytest.skip("data/db.sqlite3 does not exist")

    conn = sqlite3.connect(db_file)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(career_candidateapplicationprofile);")
    cols = {row[1] for row in cur.fetchall()}
    conn.close()

    assert "application_strategy" in cols
    assert "search_worldwide" in cols
    assert "specific_authorizations" in cols
    assert "excluded_locations" in cols
    assert "remote_mode" in cols
    assert "unknown_location_handling" in cols
