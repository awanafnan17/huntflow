from __future__ import annotations
import os
import uuid
from django.db import models
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.fields import GenericForeignKey
from openoutreach.career.storage import PrivateFileSystemStorage

private_storage = PrivateFileSystemStorage()

def candidate_doc_path(instance, filename):
    ext = os.path.splitext(filename)[1]
    # Predictable extensions only, but filename itself is random UUID
    random_filename = f"{uuid.uuid4().hex}{ext}"
    user_id = instance.document.profile.user.id
    return os.path.join(f"documents_{user_id}", random_filename)

class CandidateProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="candidate_profile")
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)
    linkedin_url = models.URLField(max_length=500, blank=True)
    summary = models.TextField(blank=True)
    active_version = models.ForeignKey("CandidateProfileVersion", on_delete=models.SET_NULL, null=True, blank=True, related_name="active_for_profile")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("view_candidate_data", "Can view candidate data"),
            ("upload_cv", "Can upload CVs"),
            ("edit_candidate_profile", "Can edit candidate profiles"),
            ("approve_profile", "Can approve profiles"),
            ("view_private_documents", "Can view private documents"),
            ("view_audit_history", "Can view audit history"),
            ("archive_candidate_document", "Can archive candidate documents"),
        ]

    def __str__(self):
        return f"Profile: {self.full_name} ({self.user.username})"

    def save(self, *args, **kwargs):
        if self.active_version and self.active_version.profile_id != self.pk:
            raise ValidationError("Active version must belong to this CandidateProfile.")
        super().save(*args, **kwargs)

class CandidateDocument(models.Model):
    DOCUMENT_TYPES = [
        ("cv", "CV / Resume"),
        ("cover_letter", "Cover Letter"),
        ("other", "Other"),
    ]
    DOCUMENT_STATUS_CHOICES = [
        ("active", "Active"),
        ("archived", "Archived"),
        ("rejected", "Rejected"),
        ("quarantined", "Quarantined"),
    ]
    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="documents")
    name = models.CharField(max_length=255)
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPES, default="cv")
    status = models.CharField(max_length=50, choices=DOCUMENT_STATUS_CHOICES, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.get_document_type_display()}: {self.name}"

class CandidateDocumentVersion(models.Model):
    document = models.ForeignKey(CandidateDocument, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    file = models.FileField(storage=private_storage, upload_to=candidate_doc_path)
    file_hash = models.CharField(max_length=64)  # SHA-256
    file_size = models.PositiveIntegerField()     # Bytes
    mime_type = models.CharField(max_length=100)
    original_filename = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("document", "version_number")
        ordering = ["-version_number"]

    def __str__(self):
        return f"{self.document.name} (v{self.version_number})"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("CandidateDocumentVersion instances are immutable.")
        super().save(*args, **kwargs)

class CandidateProfileVersion(models.Model):
    STATE_CHOICES = [
        ("draft", "Draft"),
        ("pending_approval", "Pending Approval"),
        ("approved", "Approved"),
        ("superseded", "Superseded"),
        ("rejected", "Rejected"),
    ]
    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField()
    full_name = models.CharField(max_length=200)
    email = models.EmailField()
    phone = models.CharField(max_length=50, blank=True)
    linkedin_url = models.URLField(max_length=500, blank=True)
    summary = models.TextField(blank=True)
    checksum = models.CharField(max_length=64, blank=True)
    is_finalized = models.BooleanField(default=False)
    state = models.CharField(max_length=50, choices=STATE_CHOICES, default="draft")
    fact_snapshot = models.JSONField(default=list)
    revision_token = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = ("profile", "version_number")
        ordering = ["-version_number"]

    def __str__(self):
        return f"Profile Version {self.version_number} for {self.profile.full_name}"

    def save(self, *args, **kwargs):
        if self.pk:
            old = CandidateProfileVersion.objects.get(pk=self.pk)
            if old.is_finalized:
                if (old.full_name != self.full_name or
                    old.email != self.email or
                    old.phone != self.phone or
                    old.linkedin_url != self.linkedin_url or
                    old.summary != self.summary or
                    old.checksum != self.checksum or
                    old.fact_snapshot != self.fact_snapshot):
                    raise ValidationError("Finalized CandidateProfileVersion is immutable.")
            if hasattr(self, "approval") or old.state == "approved":
                if (self.full_name != old.full_name or
                    self.email != old.email or
                    self.phone != old.phone or
                    self.linkedin_url != old.linkedin_url or
                    self.summary != old.summary or
                    self.checksum != old.checksum or
                    self.fact_snapshot != old.fact_snapshot):
                    raise ValidationError("Approved CandidateProfileVersion is immutable.")
                if self.state != "approved" and self.state != "superseded":
                    raise ValidationError("Approved version state cannot be reverted unless superseded.")
        super().save(*args, **kwargs)

class CandidateFact(models.Model):
    FACT_TYPES = [
        ("experience", "Work Experience"),
        ("education", "Education"),
        ("skill", "Skill"),
        ("certification", "Certification"),
        ("other", "Other"),
    ]
    profile_version = models.ForeignKey(CandidateProfileVersion, on_delete=models.CASCADE, related_name="facts")
    fact_type = models.CharField(max_length=50, choices=FACT_TYPES)
    category = models.CharField(max_length=100, blank=True)
    value = models.JSONField()  # e.g., {"company": "Google", "title": "SWE"}
    verified = models.BooleanField(default=True)
    source_type = models.CharField(max_length=50, default="manual")
    source_document_version = models.ForeignKey(CandidateDocumentVersion, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if self.profile_version.is_finalized:
            raise ValidationError("Facts of a finalized version cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.profile_version.is_finalized:
            raise ValidationError("Facts of a finalized version cannot be deleted.")
        super().delete(*args, **kwargs)

    def __str__(self):
        return f"Fact ({self.get_fact_type_display()}): {self.category}"

class CandidateProfileApproval(models.Model):
    profile_version = models.OneToOneField(CandidateProfileVersion, on_delete=models.CASCADE, related_name="approval")
    approved_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT)
    approval_note = models.TextField(blank=True)
    approval_reason = models.CharField(max_length=100, blank=True, default="")
    idempotency_key = models.CharField(max_length=64, unique=True)

    def __str__(self):
        return f"Approval for version {self.profile_version.version_number}"

class CandidateAuditEvent(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    event_type = models.CharField(max_length=100)
    description = models.TextField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"[{self.event_type}] {self.description[:50]}"


class CandidateExtractionRecord(models.Model):
    SOURCE_TYPES = [
        ("document", "Document Version"),
        ("raw_text", "Raw Text"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("applied", "Applied"),
        ("discarded", "Discarded"),
    ]
    profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="extractions")
    source_type = models.CharField(max_length=20, choices=SOURCE_TYPES)
    document_version = models.ForeignKey(CandidateDocumentVersion, on_delete=models.SET_NULL, null=True, blank=True)
    raw_text = models.TextField(blank=True)
    raw_text_hash = models.CharField(max_length=64, blank=True)
    extracted_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")

    def __str__(self):
        return f"Extraction ({self.source_type}) - {self.extracted_at}"


class CandidateFactDraft(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("info_needed", "Needs Info"),
    ]
    extraction_record = models.ForeignKey(CandidateExtractionRecord, on_delete=models.CASCADE, related_name="fact_drafts")
    fact_type = models.CharField(max_length=50, choices=CandidateFact.FACT_TYPES)
    category = models.CharField(max_length=100, blank=True)
    value = models.JSONField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Draft Fact ({self.fact_type}): {self.category} ({self.status})"


class Job(models.Model):
    PROVENANCE_CHOICES = [
        ("REAL_PROVIDER", "Real Provider"),
        ("IMPORTED", "Imported"),
        ("SYNTHETIC", "Synthetic"),
        ("QA", "QA"),
        ("TEST_FIXTURE", "Test Fixture"),
        ("UNKNOWN", "Unknown"),
    ]
    PROVIDER_IDENTITY_STATUS_CHOICES = [
        ("UNVERIFIED", "Unverified"),
        ("VERIFIED", "Verified"),
        ("CONFLICT", "Conflict"),
        ("EXPIRED", "Expired"),
        ("INVALID", "Invalid"),
        ("TEST_FIXTURE", "Test Fixture"),
        ("UNKNOWN", "Unknown"),
    ]
    title = models.CharField(max_length=255)
    company = models.CharField(max_length=255)
    location = models.CharField(max_length=255, blank=True)
    description = models.TextField()
    job_url = models.URLField(max_length=500, blank=True)
    source = models.CharField(max_length=100, blank=True, default="manual")
    provenance = models.CharField(max_length=50, choices=PROVENANCE_CHOICES, default="UNKNOWN")
    provider = models.CharField(max_length=50, blank=True, default="")
    external_job_id = models.CharField(max_length=100, blank=True, default="")
    canonical_job = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates")
    provider_identity_status = models.CharField(max_length=30, choices=PROVIDER_IDENTITY_STATUS_CHOICES, default="UNVERIFIED")
    provider_verification_evidence = models.JSONField(null=True, blank=True, default=dict)
    last_provider_verified_at = models.DateTimeField(null=True, blank=True)
    verification_error = models.TextField(blank=True, default="")
    captured_at = models.DateTimeField(null=True, blank=True)
    jd_hash = models.CharField(max_length=64, blank=True, default="")
    easy_apply = models.BooleanField(default=False)
    match_score = models.PositiveIntegerField(null=True, blank=True)
    match_explanation = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_synthetic = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "external_job_id"],
                condition=models.Q(canonical_job__isnull=True, provider_identity_status="VERIFIED", is_active=True) & ~models.Q(external_job_id=""),
                name="unique_canonical_verified_provider_job"
            )
        ]

    @classmethod
    def extract_canonical_provider_identity(cls, job_url: str) -> tuple[str, str, str]:
        """
        Normalize URL and extract (provider, external_job_id, canonical_url).
        E.g. https://www.linkedin.com/jobs/search/?currentJobId=4440058610 -> ('linkedin', '4440058610', 'https://www.linkedin.com/jobs/view/4440058610/')
        """
        import re
        if not job_url:
            return ("", "", "")
        url = str(job_url).strip()
        match = re.search(r'(?:/jobs/view/|currentJobId=)(\d+)', url)
        if not match:
            match = re.search(r'/jobs/(\d+)', url)
        if match:
            ext_id = match.group(1)
            canonical_url = f"https://www.linkedin.com/jobs/view/{ext_id}/"
            return ("linkedin", ext_id, canonical_url)
        return ("", "", url)

    @classmethod
    def upsert_discovered_job(
        cls,
        *,
        provider: str,
        external_job_id: str,
        title: str,
        company: str,
        location: str = "",
        description: str = "",
        job_url: str = "",
        easy_apply: bool = True,
        is_synthetic: bool = False,
        provenance: str = "REAL_PROVIDER",
        provider_identity_status: str = "UNVERIFIED"
    ) -> tuple[Job, bool]:
        """
        Concurrency-safe canonical upsert: avoids creating duplicate active jobs for the same (provider, external_job_id).
        Uses transaction.atomic and select_for_update. Defaults to UNVERIFIED status until explicit provider evidence is obtained.
        """
        import hashlib
        from django.utils import timezone
        from django.db import connection, transaction
        
        provider = (provider or "linkedin").strip().lower()
        external_job_id = str(external_job_id or "").strip()
        if not external_job_id and job_url:
            prov, ext_id, canon_url = cls.extract_canonical_provider_identity(job_url)
            if ext_id:
                provider = prov
                external_job_id = ext_id
                job_url = canon_url

        jd_hash = hashlib.sha256((description or "").strip().encode("utf-8")).hexdigest() if description else ""

        with transaction.atomic():
            if connection.vendor == "postgresql" and external_job_id:
                lock_id = int(hashlib.sha256(f"job_upsert_{provider}_{external_job_id}".encode("utf-8")).hexdigest()[:8], 16) & 0x7FFFFFFF
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", [lock_id])

            existing = None
            try:
                existing = cls.objects.select_for_update().filter(
                    provider=provider,
                    external_job_id=external_job_id,
                    canonical_job__isnull=True
                ).first()
            except Exception:
                existing = cls.objects.filter(
                    provider=provider,
                    external_job_id=external_job_id,
                    canonical_job__isnull=True
                ).first()

            if existing:
                existing.title = title or existing.title
                existing.company = company or existing.company
                if location:
                    existing.location = location
                if description:
                    existing.description = description
                    existing.jd_hash = jd_hash
                if job_url:
                    existing.job_url = job_url
                existing.easy_apply = easy_apply
                existing.is_active = True
                existing.captured_at = timezone.now()
                existing.provenance = provenance
                existing.save()
                return (existing, False)

            try:
                job = cls.objects.create(
                    title=title,
                    company=company,
                    location=location,
                    description=description,
                    job_url=job_url or f"https://www.linkedin.com/jobs/view/{external_job_id}/",
                    source=provider,
                    provider=provider,
                    external_job_id=external_job_id,
                    provenance=provenance,
                    provider_identity_status=provider_identity_status,
                    captured_at=timezone.now(),
                    jd_hash=jd_hash,
                    easy_apply=easy_apply,
                    is_active=True,
                    is_synthetic=is_synthetic
                )
                return (job, True)
            except Exception:
                existing = cls.objects.filter(
                    provider=provider,
                    external_job_id=external_job_id,
                    canonical_job__isnull=True
                ).first()
                if existing:
                    return (existing, False)
                raise

    def is_real_provider_job(self) -> bool:
        """Verify that the job record has authentic production provider provenance and verified state."""
        if not self.is_active or self.is_synthetic:
            return False
        if self.provenance in ["SYNTHETIC", "QA", "TEST_FIXTURE", "UNKNOWN"]:
            return False
        if self.provenance not in ["REAL_PROVIDER", "IMPORTED"]:
            return False
        if self.provider_identity_status in ["CONFLICT", "INVALID", "EXPIRED", "UNVERIFIED", "TEST_FIXTURE", "UNKNOWN"]:
            return False
        if self.provider_identity_status != "VERIFIED":
            return False
        if self.canonical_job_id is not None:
            return False
        url = (self.job_url or "").strip().lower()
        if not url:
            return False
        # Block test fixtures, placeholders, localhost
        blocked_domains = ["example.com", "localhost", "127.0.0.1", "test.com", "dummy"]
        if any(d in url for d in blocked_domains):
            return False
        # Must be valid LinkedIn URL format if provider is linkedin or real provider
        if "linkedin.com/jobs" not in url and not url.startswith("http"):
            return False
        if not self.title or not self.company:
            return False
        # Check for placeholder titles or companies
        placeholder_terms = ["placeholder", "dummy", "test title", "test company", "python at any"]
        if any(t in self.title.lower() for t in placeholder_terms) or any(t in self.company.lower() for t in placeholder_terms):
            return False
        return True

    def __str__(self):
        return f"{self.title} at {self.company}"


class JobApplication(models.Model):
    STATUS_CHOICES = [
        ("draft", "Draft"),
        ("approved", "Approved"),
        ("submitted", "Submitted"),
        ("failed", "Failed"),
        ("submission_unverified", "Submission Unverified"),
        ("indeterminate", "Indeterminate"),
        ("needs_your_answer", "Needs Your Answer"),
    ]
    candidate_profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="applications")
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="applications")
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="draft")
    externally_confirmed = models.BooleanField(default=False)
    confirmation_provider = models.CharField(max_length=50, blank=True, default="")
    confirmation_external_job_id = models.CharField(max_length=100, blank=True, default="")
    confirmation_evidence_type = models.CharField(max_length=100, blank=True, default="")
    confirmation_checked_at = models.DateTimeField(null=True, blank=True)
    confirmation_metadata = models.JSONField(default=dict, blank=True)
    attempted_at = models.DateTimeField(null=True, blank=True)
    unknown_questions = models.JSONField(default=list, blank=True)
    tailored_cv = models.TextField(blank=True)
    cover_letter = models.TextField(blank=True)
    audit_log = models.JSONField(default=list)
    submission_date = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("candidate_profile", "job")

    def __str__(self):
        return f"Application for {self.job} ({self.status})"


class ApplicationApproval(models.Model):
    application = models.OneToOneField(JobApplication, on_delete=models.CASCADE, related_name="approval")
    approved_cv_text = models.TextField()
    approved_cover_letter_text = models.TextField()
    cv_hash = models.CharField(max_length=64)  # SHA-256
    cover_letter_hash = models.CharField(max_length=64)  # SHA-256
    approved_at = models.DateTimeField(auto_now_add=True)
    approved_by = models.ForeignKey(User, on_delete=models.PROTECT)

    def __str__(self):
        return f"Approval for version {self.application.id}"


class DraftingBatch(models.Model):
    owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="drafting_batches")
    status = models.CharField(max_length=50, default="pending")  # pending, running, completed, failed
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Drafting Batch #{self.id} ({self.status})"


class ApplicationDraft(models.Model):
    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name="drafts")
    profile_version = models.ForeignKey(CandidateProfileVersion, on_delete=models.CASCADE, null=True, blank=True, related_name="drafts")
    batch = models.ForeignKey(DraftingBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="drafts")
    version_number = models.PositiveIntegerField()
    tailored_cv = models.TextField()
    cover_letter = models.TextField()
    structured_cv = models.JSONField(default=dict)
    traceability_map = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        unique_together = ("application", "version_number")
        ordering = ["-version_number"]

    def __str__(self):
        return f"Draft v{self.version_number} for {self.application}"

    @property
    def answers_json(self):
        if isinstance(self.structured_cv, dict):
            return self.structured_cv.get("answers", {})
        return {}

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("ApplicationDraft instances are immutable once saved.")
        super().save(*args, **kwargs)


class ApprovalBatch(models.Model):
    name = models.CharField(max_length=255, blank=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Approval Batch: {self.name or self.id}"


class ApprovalItem(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("needs_info", "Needs Information"),
        ("revoked", "Revoked"),
    ]
    batch = models.ForeignKey(ApprovalBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="items")
    application = models.ForeignKey(JobApplication, on_delete=models.CASCADE, related_name="approval_items")
    draft = models.ForeignKey(ApplicationDraft, on_delete=models.CASCADE, related_name="approval_items")
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default="pending")
    needs_info_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Approval Item for {self.application} ({self.status})"


class ApprovalHistory(models.Model):
    ACTION_CHOICES = [
        ("approve", "Approved"),
        ("reject", "Rejected"),
        ("needs_info", "Flagged Needs Info"),
        ("revoke", "Revoked"),
    ]
    item = models.ForeignKey(ApprovalItem, on_delete=models.CASCADE, related_name="history")
    actor = models.ForeignKey(User, on_delete=models.PROTECT)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    note = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.get_action_display()} by {self.actor.username} at {self.timestamp}"

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValidationError("ApprovalHistory entries are immutable.")
        super().save(*args, **kwargs)


class JobVersion(models.Model):
    job = models.ForeignKey(Job, on_delete=models.CASCADE, related_name="versions")
    version_number = models.PositiveIntegerField(default=1)
    title = models.CharField(max_length=255)
    company = models.CharField(max_length=255)
    description = models.TextField()
    canonical_url = models.URLField(max_length=500, blank=True)
    content_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} at {self.company} (v{self.version_number})"


class JobMatch(models.Model):
    candidate_profile_version = models.ForeignKey(CandidateProfileVersion, on_delete=models.CASCADE, related_name="matches")
    job_version = models.ForeignKey(JobVersion, on_delete=models.CASCADE, related_name="matches")
    overall_score = models.PositiveIntegerField(default=0)
    required_skills_score = models.PositiveIntegerField(default=0)
    preferred_skills_score = models.PositiveIntegerField(default=0)
    experience_score = models.PositiveIntegerField(default=0)
    education_score = models.PositiveIntegerField(default=0)
    location_score = models.PositiveIntegerField(default=0)
    work_authorization_result = models.CharField(max_length=100, blank=True)
    remote_preference_result = models.CharField(max_length=100, blank=True)
    strengths = models.TextField(blank=True)
    gaps = models.TextField(blank=True)
    missing_requirements = models.TextField(blank=True)
    recommendation = models.TextField(blank=True)
    warnings = models.TextField(blank=True)
    is_stale = models.BooleanField(default=False)
    input_checksum = models.CharField(max_length=64, blank=True)
    result_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Match: Candidate {self.candidate_profile_version_id} with JobVersion {self.job_version_id} ({self.overall_score})"


class IncidentHold(models.Model):
    reason = models.CharField(max_length=100, default="IDENTITY_AND_SOURCE_INTEGRITY_INCIDENT")
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    active = models.BooleanField(default=True)

    def __str__(self):
        return f"Hold: {self.reason} (Active: {self.active})"


class IncidentHoldTarget(models.Model):
    incident_hold = models.ForeignKey(IncidentHold, on_delete=models.CASCADE, related_name="targets")
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE)
    object_id = models.PositiveIntegerField()
    content_object = GenericForeignKey("content_type", "object_id")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("incident_hold", "content_type", "object_id")

    def __str__(self):
        return f"Hold target {self.content_type} ID {self.object_id} under Hold {self.incident_hold_id}"


class CandidateIdentityVerification(models.Model):
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("verified", "Verified"),
        ("rejected", "Rejected"),
    ]
    candidate_profile = models.ForeignKey("CandidateProfile", on_delete=models.CASCADE, related_name="verifications")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending")
    extracted_name = models.CharField(max_length=200)
    confirmed_name = models.CharField(max_length=200)
    verification_source = models.CharField(max_length=100)
    source_document_id = models.IntegerField(null=True, blank=True)
    source_document_version_id = models.IntegerField(null=True, blank=True)
    evidence_excerpt = models.TextField(blank=True)
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_notes = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["candidate_profile"],
                condition=models.Q(status="verified"),
                name="unique_verified_identity_per_profile"
            )
        ]

    def clean(self):
        if self.status == "verified":
            qs = CandidateIdentityVerification.objects.filter(
                candidate_profile=self.candidate_profile,
                status="verified"
            )
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError("A candidate profile may have only one active verified identity.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Identity Verification ({self.status}) for {self.candidate_profile}"


class CandidateIdentityCorrectionLedger(models.Model):
    STATUS_CHOICES = [
        ("pending_review", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
        ("superseded", "Superseded"),
        ("cancelled", "Cancelled"),
    ]
    DISCREPANCY_CHOICES = [
        ("not_applicable", "Not Applicable"),
        ("unresolved", "Unresolved"),
        ("resolved_use_profile_email", "Resolved (Use Profile Email)"),
        ("resolved_use_source_email", "Resolved (Use Source Email)"),
        ("rejected", "Rejected"),
    ]

    profile = models.ForeignKey(CandidateProfile, on_delete=models.PROTECT)
    corrected_version = models.OneToOneField(
        CandidateProfileVersion, on_delete=models.PROTECT, related_name="correction_ledger"
    )
    historical_version = models.ForeignKey(
        CandidateProfileVersion, on_delete=models.PROTECT, related_name="corrected_by_ledgers"
    )
    source_document_version = models.ForeignKey(CandidateDocumentVersion, on_delete=models.PROTECT)
    extraction_record = models.ForeignKey(CandidateExtractionRecord, on_delete=models.PROTECT)
    idempotency_key = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.PROTECT)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending_review")
    source_file_hash = models.CharField(max_length=64)
    source_text_checksum = models.CharField(max_length=64)
    normalized_source_identity = models.CharField(max_length=200)
    source_email = models.CharField(max_length=200, null=True, blank=True)
    preserved_profile_email = models.CharField(max_length=200)
    contact_email_discrepancy_status = models.CharField(
        max_length=50, choices=DISCREPANCY_CHOICES, default="unresolved"
    )
    historical_fact_checksum = models.CharField(max_length=64)
    corrected_fact_checksum = models.CharField(max_length=64)
    corrected_payload_checksum = models.CharField(max_length=64)
    request_fingerprint = models.CharField(max_length=64)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["profile"],
                condition=models.Q(status="pending_review"),
                name="unique_pending_identity_correction_per_profile"
            ),
            models.UniqueConstraint(
                fields=["profile", "idempotency_key"],
                name="unique_identity_correction_key_per_profile"
            )
        ]

    def clean(self):
        if self.corrected_version.profile != self.profile:
            raise ValidationError("Corrected version profile mismatch.")
        if self.historical_version.profile != self.profile:
            raise ValidationError("Historical version profile mismatch.")
        if self.source_document_version.document.profile != self.profile:
            raise ValidationError("Source document profile mismatch.")
        if self.extraction_record.document_version != self.source_document_version:
            raise ValidationError("Extraction record document version mismatch.")

    def save(self, *args, **kwargs):
        self.full_clean()
        if self.pk:
            old = CandidateIdentityCorrectionLedger.objects.get(pk=self.pk)
            immutable_fields = [
                "profile_id", "corrected_version_id", "historical_version_id",
                "source_document_version_id", "extraction_record_id",
                "source_file_hash", "source_text_checksum", "normalized_source_identity",
                "source_email", "preserved_profile_email",
                "historical_fact_checksum", "corrected_fact_checksum",
                "corrected_payload_checksum", "created_by_id", "idempotency_key",
                "request_fingerprint"
            ]
            for field in immutable_fields:
                if getattr(old, field) != getattr(self, field):
                    raise ValidationError(f"Field '{field}' of CandidateIdentityCorrectionLedger is immutable.")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Correction ledger for {self.profile} ({self.status})"


class JobSearchPreference(models.Model):
    EXECUTION_MODE_CHOICES = [
        ("discover_only", "Discover Only"),
        ("queue_for_review", "Queue for Review"),
        ("apply_when_authorized", "Apply When Authorized"),
    ]

    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, related_name="job_search_preferences")
    campaign = models.OneToOneField("core.Campaign", on_delete=models.CASCADE, related_name="job_search_preference")
    candidate_profile = models.ForeignKey("CandidateProfile", on_delete=models.CASCADE, related_name="job_search_preferences")
    candidate_profile_version = models.ForeignKey("CandidateProfileVersion", on_delete=models.CASCADE, related_name="job_search_preferences")

    keywords = models.JSONField(default=list, blank=True)
    job_titles = models.JSONField(default=list, blank=True)
    locations = models.JSONField(default=list, blank=True)
    remote_options = models.JSONField(default=list, blank=True)
    employment_types = models.JSONField(default=list, blank=True)
    experience_levels = models.JSONField(default=list, blank=True)
    date_posted = models.CharField(max_length=50, default="any_time")
    easy_apply_only = models.BooleanField(default=False)
    included_companies = models.JSONField(default=list, blank=True)
    excluded_companies = models.JSONField(default=list, blank=True)
    included_industries = models.JSONField(default=list, blank=True)
    excluded_industries = models.JSONField(default=list, blank=True)
    minimum_salary = models.PositiveIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=10, default="USD")
    work_authorization_required = models.BooleanField(default=False)
    visa_sponsorship_required = models.BooleanField(default=False)
    match_threshold = models.PositiveIntegerField(
        default=75,
        validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    execution_mode = models.CharField(
        max_length=50,
        choices=EXECUTION_MODE_CHOICES,
        default="discover_only"
    )
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.match_threshold < 0 or self.match_threshold > 100:
            raise ValidationError("match_threshold must be between 0 and 100.")
        if self.candidate_profile_version.profile_id != self.candidate_profile_id:
            raise ValidationError("CandidateProfileVersion mismatch for selected CandidateProfile.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"JobSearchPreference for Campaign {self.campaign_id} (Threshold: {self.match_threshold}%)"


class JobMatchAssessment(models.Model):
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, null=True, blank=True, related_name="job_match_assessments")
    campaign = models.ForeignKey("core.Campaign", on_delete=models.CASCADE, null=True, blank=True, related_name="job_match_assessments")
    job = models.ForeignKey("Job", on_delete=models.CASCADE, related_name="match_assessments")
    candidate_profile = models.ForeignKey("CandidateProfile", on_delete=models.CASCADE, related_name="match_assessments")
    candidate_profile_version = models.ForeignKey("CandidateProfileVersion", on_delete=models.CASCADE, related_name="match_assessments")

    score = models.FloatField()
    threshold = models.PositiveIntegerField()
    eligible = models.BooleanField(default=False)
    hard_blocked = models.BooleanField(default=False)
    hard_block_reasons = models.JSONField(default=list, blank=True)
    breakdown = models.JSONField(default=dict, blank=True)
    matched_evidence = models.JSONField(default=list, blank=True)
    missing_requirements = models.JSONField(default=list, blank=True)
    cv_checksum = models.CharField(max_length=64)
    job_checksum = models.CharField(max_length=64)
    scorer_version = models.CharField(max_length=20, default="v1.0")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "candidate_profile_version", "cv_checksum", "job_checksum", "scorer_version"],
                name="unique_job_match_assessment_per_version_and_checksums"
            )
        ]

    def __str__(self):
        return f"Assessment for Job {self.job_id} ({self.score:.1f}% vs {self.threshold}%)"


class CandidateApplicationProfile(models.Model):
    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, null=True, blank=True, related_name="candidate_app_profiles")
    candidate_profile = models.OneToOneField(CandidateProfile, on_delete=models.CASCADE, related_name="application_profile")
    
    # Personal Identity
    full_name = models.CharField(max_length=200, blank=True, default="")
    preferred_name = models.CharField(max_length=200, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=50, blank=True, default="")
    city = models.CharField(max_length=100, blank=True, default="")
    country = models.CharField(max_length=100, blank=True, default="")
    postal_code = models.CharField(max_length=20, blank=True, default="")
    
    # Links
    linkedin_url = models.URLField(max_length=500, blank=True, default="")
    github_url = models.URLField(max_length=500, blank=True, default="")
    portfolio_url = models.URLField(max_length=500, blank=True, default="")
    website_url = models.URLField(max_length=500, blank=True, default="")

    # Job Preferences & Work Mode
    target_job_titles = models.JSONField(default=list, blank=True)
    target_locations = models.JSONField(default=list, blank=True)
    excluded_locations = models.JSONField(default=list, blank=True)
    remote_mode = models.CharField(max_length=30, default="allow_all", blank=True)
    unknown_location_handling = models.CharField(max_length=20, default="deny", blank=True)
    work_modes = models.JSONField(default=list, blank=True)  # remote, hybrid, onsite
    employment_types = models.JSONField(default=list, blank=True)  # full_time, part_time, contract, internship
    willing_to_relocate = models.BooleanField(default=False)
    relocation_willingness = models.CharField(max_length=30, default="unknown")  # yes, no, depends, unknown
    remote_willingness = models.CharField(max_length=20, default="unknown")  # yes, no, unknown
    hybrid_willingness = models.CharField(max_length=20, default="unknown")  # yes, no, unknown
    onsite_willingness = models.CharField(max_length=20, default="unknown")  # yes, no, depends, unknown
    travel_willingness = models.CharField(max_length=20, default="unknown")  # yes, no, depends, unknown
    background_check_willingness = models.CharField(max_length=30, default="unknown")  # yes, no, ask_each_time, unknown
    earliest_start_date = models.CharField(max_length=100, blank=True, default="")
    notice_period = models.CharField(max_length=100, blank=True, default="")

    # Application Strategy & Global Targeting
    application_strategy = models.CharField(
        max_length=40,
        choices=[
            ("INTERVIEW_FIRST_SPONSORSHIP", "Interview First — Sponsorship / Authorization Later"),
            ("STRICT_AUTHORIZATION", "Strict Work Authorization")
        ],
        default="INTERVIEW_FIRST_SPONSORSHIP",
        blank=True
    )
    search_worldwide = models.BooleanField(default=False)

    def get_location_targeting_policy(self):
        from openoutreach.core.services.location_targeting import LocationTargetingPolicy
        return LocationTargetingPolicy(
            included_locations=list(self.target_locations or []),
            excluded_locations=list(self.excluded_locations or []),
            remote_mode=self.remote_mode or "allow_all",
            allow_hybrid=(self.hybrid_willingness != "no"),
            allow_onsite=(self.onsite_willingness != "no"),
            unknown_location_handling=self.unknown_location_handling or "deny",
            application_strategy=self.application_strategy or "INTERVIEW_FIRST_SPONSORSHIP",
            search_worldwide=bool(self.search_worldwide),
            channel="jobs",
        )

    # Work Authorization
    authorized_countries = models.JSONField(default=list, blank=True)
    specific_authorizations = models.JSONField(default=dict, blank=True)
    requires_sponsorship_now = models.BooleanField(default=False)
    requires_sponsorship_future = models.BooleanField(default=False)
    visa_sponsorship_now = models.CharField(max_length=20, default="unknown")  # yes, no, depends, unknown
    visa_sponsorship_future = models.CharField(max_length=20, default="unknown")  # yes, no, depends, unknown

    # Compensation
    currency = models.CharField(max_length=10, default="USD")
    minimum_salary = models.PositiveIntegerField(null=True, blank=True)
    target_salary = models.PositiveIntegerField(null=True, blank=True)
    salary_period = models.CharField(max_length=20, default="annual")  # annual, monthly, hourly
    allow_automatic_salary = models.BooleanField(default=False)
    salary_negotiable = models.BooleanField(default=True)

    # Education & Experience Summaries
    education_summary = models.JSONField(default=list, blank=True)
    skill_experience_years = models.JSONField(default=dict, blank=True)
    languages = models.JSONField(default=list, blank=True)

    # Confirmation Audit
    confirmed = models.BooleanField(default=False)
    confirmed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=100, default="manual_questionnaire")
    profile_version = models.CharField(max_length=50, default="v1.0")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def document_confirmed(self) -> bool:
        return self.confirmed

    @document_confirmed.setter
    def document_confirmed(self, value: bool):
        self.confirmed = bool(value)

    def __str__(self):
        return f"Application Profile for {self.candidate_profile}"


class JobApplicationAnswer(models.Model):
    ANSWER_TYPE_CHOICES = [
        ("boolean", "Boolean"),
        ("text", "Text"),
        ("number", "Number"),
        ("date", "Date"),
        ("choice", "Single Choice"),
        ("multi_choice", "Multiple Choice"),
    ]

    workspace = models.ForeignKey("core.Workspace", on_delete=models.CASCADE, null=True, blank=True, related_name="job_application_answers")
    candidate_profile = models.ForeignKey(CandidateProfile, on_delete=models.CASCADE, related_name="answers")
    campaign = models.ForeignKey("core.Campaign", on_delete=models.CASCADE, null=True, blank=True, related_name="answers")
    job = models.ForeignKey("Job", on_delete=models.CASCADE, null=True, blank=True, related_name="answers")

    canonical_key = models.CharField(max_length=150, db_index=True)
    answer_type = models.CharField(max_length=50, choices=ANSWER_TYPE_CHOICES, default="text")
    answer_value = models.JSONField()

    confirmed = models.BooleanField(default=True)
    confirmed_at = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=100, default="operator_confirmed")

    last_used_at = models.DateTimeField(null=True, blank=True)
    usage_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["candidate_profile", "campaign", "job", "canonical_key"],
                name="unique_answer_key_per_scope"
            )
        ]

    def __str__(self):
        return f"Answer[{self.canonical_key}] = {self.answer_value} ({self.candidate_profile})"





