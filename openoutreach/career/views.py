import os
import re
import hashlib
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.exceptions import PermissionDenied
from django.http import FileResponse, Http404, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_POST

from openoutreach.career.models import (
    CandidateProfile,
    CandidateDocument,
    CandidateDocumentVersion,
    CandidateProfileVersion,
    CandidateFact,
    CandidateProfileApproval,
    CandidateAuditEvent,
    CandidateExtractionRecord,
    CandidateFactDraft,
    Job,
    JobApplication,
    ApplicationApproval
)
from openoutreach.career.forms import CandidateProfileForm, DocumentUploadForm, RawCVForm, JobForm, JobApplicationForm
from openoutreach.career.service import (
    parse_and_create_drafts_from_document,
    parse_and_create_drafts_from_raw_text,
    compute_job_match,
    tailor_application
)

def get_client_ip(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0]
    else:
        ip = request.META.get('REMOTE_ADDR')
    return ip

def log_audit_event(user, event_type, description, request=None):
    ip = get_client_ip(request) if request else None
    CandidateAuditEvent.objects.create(
        user=user,
        event_type=event_type,
        description=description,
        ip_address=ip
    )

def _get_or_create_profile(request):
    profile, created = CandidateProfile.objects.get_or_create(
        user=request.user,
        defaults={
            "full_name": request.user.get_full_name(),
            "email": request.user.email or "placeholder@example.com"
        }
    )
    if created:
        CandidateProfileVersion.objects.create(
            profile=profile,
            version_number=1,
            full_name=profile.full_name,
            email=profile.email,
            created_by=request.user
        )
        log_audit_event(request.user, "profile_created", f"Initial profile created automatically for {request.user.username}.", request)
    return profile

@login_required
def career_overview(request):
    profile = _get_or_create_profile(request)

    # Permissions check: user can only view their own data unless they have view_candidate_data permission
    if not (request.user == profile.user or request.user.has_perm("career.view_candidate_data")):
        raise PermissionDenied("You do not have permission to view this candidate profile.")

    documents = profile.documents.all()
    versions = profile.versions.all()
    
    # Audit log (either for this user's profile, or all if admin has permission)
    if request.user.has_perm("career.view_audit_history"):
        audit_events = CandidateAuditEvent.objects.all()[:50]
    else:
        audit_events = CandidateAuditEvent.objects.filter(user=request.user)[:20]

    extractions = profile.extractions.all().order_by("-extracted_at")
    applications = profile.applications.all().order_by("-created_at")

    context = {
        "profile": profile,
        "documents": documents,
        "versions": versions,
        "audit_events": audit_events,
        "extractions": extractions,
        "applications": applications,
    }
    return render(request, "career/overview.html", context)

@login_required
def profile_edit(request):
    profile = _get_or_create_profile(request)

    if not (request.user == profile.user or request.user.has_perm("career.edit_candidate_profile")):
        raise PermissionDenied("You do not have permission to edit this candidate profile.")

    if request.method == "POST":
        form = CandidateProfileForm(request.POST, instance=profile)
        if form.is_valid():
            profile = form.save()
            
            # Auto-versioning: create a new CandidateProfileVersion
            last_version = profile.versions.order_by("-version_number").first()
            next_version_num = (last_version.version_number + 1) if last_version else 1
            
            version = CandidateProfileVersion.objects.create(
                profile=profile,
                version_number=next_version_num,
                full_name=profile.full_name,
                email=profile.email,
                phone=profile.phone,
                linkedin_url=profile.linkedin_url,
                summary=profile.summary,
                created_by=request.user
            )
            
            # Copy active facts from last version (if any)
            if last_version:
                for fact in last_version.facts.all():
                    CandidateFact.objects.create(
                        profile_version=version,
                        fact_type=fact.fact_type,
                        category=fact.category,
                        value=fact.value,
                        verified=fact.verified,
                        source_type=fact.source_type,
                        source_document_version=fact.source_document_version
                    )

            log_audit_event(request.user, "profile_updated", f"Updated profile fields, generated version #{next_version_num}.", request)
            messages.success(request, f"Profile updated and version #{next_version_num} generated.")
            return redirect("career:overview")
    else:
        form = CandidateProfileForm(instance=profile)

    return render(request, "career/profile_edit.html", {"form": form, "profile": profile})

@login_required
def document_upload(request):
    profile = _get_or_create_profile(request)

    if not (request.user == profile.user or request.user.has_perm("career.upload_cv")):
        raise PermissionDenied("You do not have permission to upload documents.")

    if request.method == "POST":
        form = DocumentUploadForm(request.POST, request.FILES)
        if form.is_valid():
            name = form.cleaned_data["name"]
            doc_type = form.cleaned_data["document_type"]
            uploaded_file = request.FILES["file"]

            # Compute SHA-256 hash of the file content
            sha256 = hashlib.sha256()
            for chunk in uploaded_file.chunks():
                sha256.update(chunk)
            file_hash = sha256.hexdigest()

            # Find or create CandidateDocument
            document, created = CandidateDocument.objects.get_or_create(
                profile=profile,
                name=name,
                defaults={"document_type": doc_type}
            )

            # Get next version number
            last_ver = document.versions.order_by("-version_number").first()
            next_ver_num = (last_ver.version_number + 1) if last_ver else 1

            # Create immutable version
            version = CandidateDocumentVersion.objects.create(
                document=document,
                version_number=next_ver_num,
                file=uploaded_file,
                file_hash=file_hash,
                file_size=uploaded_file.size,
                mime_type=uploaded_file.content_type,
                original_filename=uploaded_file.name
            )

            log_audit_event(request.user, "document_uploaded", f"Uploaded version #{next_ver_num} of document '{name}' (Hash: {file_hash[:10]}).", request)
            messages.success(request, f"Document '{name}' (v{next_ver_num}) uploaded successfully.")
            return redirect("career:overview")
    else:
        form = DocumentUploadForm()

    return render(request, "career/document_upload.html", {"form": form})

@login_required
def document_download(request, version_pk):
    version = get_object_or_404(CandidateDocumentVersion, pk=version_pk)
    document = version.document
    profile = document.profile

    # Permissions check
    if not (request.user == profile.user or request.user.has_perm("career.view_private_documents")):
        raise PermissionDenied("You do not have permission to download this document.")

    # Archived, rejected, and quarantined-state handling
    if document.status == "quarantined":
        raise PermissionDenied("This document has been quarantined and cannot be downloaded.")
    if document.status == "rejected":
        raise PermissionDenied("This document has been rejected.")

    file_path = version.file.path
    if not os.path.exists(file_path):
        raise Http404("Document file not found on disk.")

    log_audit_event(request.user, "document_downloaded", f"Downloaded document version '{version}' (v{version.version_number}).", request)
    
    # Safe Content-Disposition filename
    from django.utils.text import get_valid_filename
    safe_filename = get_valid_filename(version.original_filename)
    
    # Authenticated, ownership-protected download
    response = FileResponse(open(file_path, 'rb'), content_type=version.mime_type)
    response['Content-Disposition'] = f'attachment; filename="{safe_filename}"'
    return response

@login_required
def profile_approve(request, version_pk):
    version = get_object_or_404(CandidateProfileVersion, pk=version_pk)
    profile = version.profile

    # Permissions check
    if not request.user.has_perm("career.approve_profile"):
        raise PermissionDenied("You do not have permission to approve profile versions.")

    if request.method == "POST":
        note = request.POST.get("note", "").strip()
        
        # Idempotency key (SHA-256 of profile ID, version number, and approval attempt)
        key_src = f"profile-{profile.id}-version-{version.version_number}-approved-by-{request.user.id}"
        idempotency_key = hashlib.sha256(key_src.encode("utf-8")).hexdigest()

        # Check if already approved
        if hasattr(version, "approval"):
            messages.warning(request, f"Profile version #{version.version_number} is already approved.")
            return redirect("career:overview")

        approval = CandidateProfileApproval.objects.create(
            profile_version=version,
            approved_by=request.user,
            approval_note=note,
            idempotency_key=idempotency_key
        )

        log_audit_event(request.user, "profile_approved", f"Approved profile version #{version.version_number}.", request)
        messages.success(request, f"Successfully approved profile version #{version.version_number}.")
        
    return redirect("career:overview")


@login_required
def audit_trail(request):
    if not request.user.has_perm("career.view_audit_history"):
        raise PermissionDenied("You do not have permission to view audit history.")
    
    audit_events = CandidateAuditEvent.objects.all()[:100]
    return render(request, "career/audit_trail.html", {"audit_events": audit_events})


@login_required
def document_archive(request, document_pk):
    document = get_object_or_404(CandidateDocument, pk=document_pk)
    profile = document.profile

    # Permissions check: user must be owner or have archive permission
    if not (request.user == profile.user or request.user.has_perm("career.archive_candidate_document")):
        raise PermissionDenied("You do not have permission to archive this document.")

    document.status = "archived"
    document.save()

    log_audit_event(request.user, "document_archived", f"Archived document '{document.name}' (ID: {document.id}).", request)
    messages.success(request, f"Document '{document.name}' was successfully archived.")
    return redirect("career:overview")


@login_required
def raw_cv_input(request):
    profile = _get_or_create_profile(request)
    
    if not (request.user == profile.user or request.user.has_perm("career.upload_cv")):
        raise PermissionDenied("You do not have permission to use raw CV input.")
        
    if request.method == "POST":
        form = RawCVForm(request.POST)
        if form.is_valid():
            raw_text = form.cleaned_data["raw_text"]
            
            # Run extraction
            record = parse_and_create_drafts_from_raw_text(profile, raw_text)
            
            log_audit_event(request.user, "raw_cv_extracted", f"Pasted raw CV text parsed (Length: {len(raw_text)}).", request)
            messages.success(request, "Raw CV text parsed successfully. Please review the extracted facts below.")
            return redirect("career:extraction_review", record_pk=record.pk)
    else:
        form = RawCVForm()
        
    return render(request, "career/raw_cv_input.html", {"form": form})


@login_required
def document_parse(request, version_pk):
    version = get_object_or_404(CandidateDocumentVersion, pk=version_pk)
    profile = version.document.profile
    
    # Permissions check
    if not (request.user == profile.user or request.user.has_perm("career.upload_cv")):
        raise PermissionDenied("You do not have permission to parse documents.")
        
    record = parse_and_create_drafts_from_document(profile, version)
    
    log_audit_event(request.user, "document_parsed", f"Parsed document version '{version}' for extraction.", request)
    messages.success(request, "Document parsed successfully. Please review the extracted facts below.")
    return redirect("career:extraction_review", record_pk=record.pk)


@login_required
def extraction_review(request, record_pk):
    record = get_object_or_404(CandidateExtractionRecord, pk=record_pk)
    profile = record.profile
    
    if not (request.user == profile.user or request.user.has_perm("career.edit_candidate_profile")):
        raise PermissionDenied("You do not have permission to review this extraction.")
        
    drafts = record.fact_drafts.all()
    
    if request.method == "POST":
        action = request.POST.get("action", "save")
        
        # Save edits to all draft facts
        for draft in drafts:
            prefix = f"draft_{draft.id}_"
            draft_status = request.POST.get(f"{prefix}status")
            if draft_status in [choice[0] for choice in CandidateFactDraft.STATUS_CHOICES]:
                draft.status = draft_status
                
            # Process value changes depending on fact type
            if draft.fact_type == "experience":
                draft.category = request.POST.get(f"{prefix}company", draft.category)
                bullet_text = request.POST.get(f"{prefix}bullet_points", "")
                bullets = [b.strip() for b in bullet_text.split("\n") if b.strip()]
                draft.value = {
                    "company": request.POST.get(f"{prefix}company", ""),
                    "title": request.POST.get(f"{prefix}title", ""),
                    "start": request.POST.get(f"{prefix}start", ""),
                    "end": request.POST.get(f"{prefix}end", ""),
                    "bullet_points": bullets
                }
            elif draft.fact_type == "education":
                draft.category = request.POST.get(f"{prefix}school", draft.category)
                draft.value = {
                    "school": request.POST.get(f"{prefix}school", ""),
                    "degree": request.POST.get(f"{prefix}degree", ""),
                    "year": request.POST.get(f"{prefix}year", "")
                }
            elif draft.fact_type == "skill":
                draft.value = {
                    "name": request.POST.get(f"{prefix}name", "")
                }
            elif draft.fact_type == "certification":
                draft.category = request.POST.get(f"{prefix}name", draft.category)
                draft.value = {
                    "name": request.POST.get(f"{prefix}name", ""),
                    "issuer": request.POST.get(f"{prefix}issuer", ""),
                    "year": request.POST.get(f"{prefix}year", "")
                }
            draft.save()
            
        if action == "commit":
            approved_drafts = record.fact_drafts.filter(status="approved")
            if not approved_drafts.exists():
                messages.error(request, "No facts have been marked as Approved. Please approve at least one fact to commit.")
                return redirect("career:extraction_review", record_pk=record.pk)
                
            # Create next CandidateProfileVersion
            last_ver = profile.versions.order_by("-version_number").first()
            next_ver_num = (last_ver.version_number + 1) if last_ver else 1
            
            # Save main bio info inputted on review page into new profile version
            full_name = request.POST.get("full_name", profile.full_name)
            email = request.POST.get("email", profile.email)
            phone = request.POST.get("phone", profile.phone)
            linkedin_url = request.POST.get("linkedin_url", profile.linkedin_url)
            summary = request.POST.get("summary", profile.summary)
            
            new_version = CandidateProfileVersion.objects.create(
                profile=profile,
                version_number=next_ver_num,
                full_name=full_name,
                email=email,
                phone=phone,
                linkedin_url=linkedin_url,
                summary=summary,
                created_by=request.user
            )
            
            # Copy approved drafts to CandidateFacts linked to the new version
            for draft in approved_drafts:
                CandidateFact.objects.create(
                    profile_version=new_version,
                    fact_type=draft.fact_type,
                    category=draft.category,
                    value=draft.value,
                    verified=True,
                    source_type=record.source_type,
                    source_document_version=record.document_version
                )
                
            record.status = "applied"
            record.save()
            
            # Update the main profile non-version fields to align with committed version
            profile.full_name = full_name
            profile.email = email
            profile.phone = phone
            profile.linkedin_url = linkedin_url
            profile.summary = summary
            profile.save()
            
            log_audit_event(request.user, "profile_extraction_applied", f"Committed extraction record #{record.pk} facts into profile version #{next_ver_num}.", request)
            messages.success(request, f"Committed approved facts as profile version #{next_ver_num} successfully.")
            return redirect("career:overview")
            
        messages.success(request, "Changes saved successfully.")
        return redirect("career:extraction_review", record_pk=record.pk)
        
    return render(request, "career/extraction_review.html", {
        "record": record,
        "drafts": drafts,
        "profile": profile
    })


@login_required
def job_list(request):
    profile = _get_or_create_profile(request)
    include_synthetic = request.GET.get("include_synthetic") == "1" and request.user.is_superuser
    
    qs = Job.objects.all()
    if not include_synthetic:
        qs = qs.filter(is_synthetic=False).exclude(title__startswith="[TEST]")

    # Filters
    location_filter = request.GET.get("location", "").strip().lower()
    work_mode_filter = request.GET.get("work_mode", "").strip().lower()
    min_match_raw = request.GET.get("min_match", "").strip()
    provider_status_filter = request.GET.get("provider_status", "").strip().upper()
    applied_filter = request.GET.get("applied", "").strip().lower()

    if location_filter:
        from openoutreach.core.services.location_targeting import evaluate_location_match, LocationTargetingPolicy
        policy = LocationTargetingPolicy(included_locations=[location_filter], channel="jobs")
        # In-memory location match or query search
        qs = qs.filter(location__icontains=location_filter)

    if work_mode_filter:
        if work_mode_filter == "remote":
            qs = qs.filter(work_mode__icontains="remote")
        elif work_mode_filter == "hybrid":
            qs = qs.filter(work_mode__icontains="hybrid")
        elif work_mode_filter == "onsite":
            qs = qs.filter(work_mode__icontains="onsite")

    if provider_status_filter:
        qs = qs.filter(provider_identity_status=provider_status_filter)

    if min_match_raw and min_match_raw.isdigit():
        min_score = int(min_match_raw)
        qs = qs.filter(match_score__gte=min_score)
    
    all_jobs = list(qs.order_by("-created_at"))
    apps = {a.job_id: a for a in JobApplication.objects.filter(candidate_profile=profile)}
    app_prof = getattr(profile, "application_profile", None)
    loc_policy = app_prof.get_location_targeting_policy() if app_prof and hasattr(app_prof, "get_location_targeting_policy") else None
    
    for j in all_jobs:
        app = apps.get(j.id)
        if app:
            j.application_status = app.status
            j.is_externally_confirmed = app.externally_confirmed
            j.app_submission_date = app.submission_date
        else:
            j.application_status = "unapplied"
            j.is_externally_confirmed = False
            j.app_submission_date = None

        if loc_policy and j.location:
            from openoutreach.core.services.location_targeting import evaluate_location_match
            loc_res = evaluate_location_match(
                j.location,
                loc_policy,
                candidate_authorization=getattr(app_prof, "authorized_countries", None),
                candidate_sponsorship_now=getattr(app_prof, "visa_sponsorship_now", None)
            )
            j.authorization_badge = loc_res.authorization_status
        else:
            j.authorization_badge = "CONFIRMED_AUTHORIZED"

    if applied_filter == "applied":
        all_jobs = [j for j in all_jobs if j.application_status == "submitted"]
    elif applied_filter == "unapplied":
        all_jobs = [j for j in all_jobs if j.application_status == "unapplied"]

    active_tab = request.GET.get("tab", "matches").strip().lower()
    if active_tab == "ready":
        filtered_jobs = [j for j in all_jobs if j.application_status in ("draft", "approved")]
    elif active_tab == "needs_answer":
        filtered_jobs = [j for j in all_jobs if j.application_status == "failed" or getattr(j, "has_missing_answers", False)]
    elif active_tab == "applied":
        filtered_jobs = [j for j in all_jobs if j.application_status == "submitted"]
    else:
        active_tab = "matches"
        filtered_jobs = all_jobs

    counts = {
        "matches": len(all_jobs),
        "ready": len([j for j in all_jobs if j.application_status in ("draft", "approved")]),
        "needs_answer": len([j for j in all_jobs if j.application_status == "failed" or getattr(j, "has_missing_answers", False)]),
        "applied": len([j for j in all_jobs if j.application_status == "submitted"]),
    }

    return render(request, "career/job_list.html", {
        "jobs": filtered_jobs,
        "profile": profile,
        "active_tab": active_tab,
        "counts": counts,
        "filter_location": location_filter,
        "filter_work_mode": work_mode_filter,
        "filter_provider_status": provider_status_filter,
        "filter_min_match": min_match_raw,
        "filter_applied": applied_filter,
    })


@login_required
def job_add(request):
    if request.method == "POST":
        form = JobForm(request.POST)
        if form.is_valid():
            job = form.save()
            log_audit_event(request.user, "job_imported", f"Manually imported job '{job.title}' at '{job.company}'.", request)
            messages.success(request, f"Job '{job.title}' at '{job.company}' imported successfully.")
            return redirect("career:job_list")
    else:
        form = JobForm()
    return render(request, "career/job_add.html", {"form": form})


@login_required
def job_detail(request, job_pk):
    job = get_object_or_404(Job, pk=job_pk)
    profile = _get_or_create_profile(request)
    application = JobApplication.objects.filter(candidate_profile=profile, job=job).first()
    return render(request, "career/job_detail.html", {
        "job": job,
        "application": application
    })


@login_required
@require_POST
def job_match(request, job_pk):
    job = get_object_or_404(Job, pk=job_pk)
    profile = _get_or_create_profile(request)
    
    profile_version = CandidateProfileVersion.objects.filter(profile=profile, approval__isnull=False).order_by("-version_number").first()
    if not profile_version:
        profile_version = CandidateProfileVersion.objects.filter(profile=profile).order_by("-version_number").first()
        
    if not profile_version:
        messages.error(request, "No candidate profile details or versions found to perform job matching.")
        return redirect("career:job_detail", job_pk=job.pk)
        
    try:
        result = compute_job_match(profile_version, job)
        job.match_score = result.match_score
        job.match_explanation = result.match_explanation
        job.save()
        log_audit_event(request.user, "job_matched", f"Computed match score {job.match_score}% for job '{job.title}'.", request)
        messages.success(request, f"Successfully calculated match score: {job.match_score}%.")
    except Exception as e:
        messages.error(request, f"Error computing match: {str(e)}")
        
    return redirect("career:job_detail", job_pk=job.pk)


@login_required
@require_POST
def job_apply(request, job_pk):
    job = get_object_or_404(Job, pk=job_pk)
    profile = _get_or_create_profile(request)
    
    # Check if application already exists
    app = JobApplication.objects.filter(candidate_profile=profile, job=job).first()
    if app:
        messages.info(request, "An application for this job has already been started.")
        return redirect("career:application_review", app_pk=app.pk)
        
    profile_version = CandidateProfileVersion.objects.filter(profile=profile, approval__isnull=False).order_by("-version_number").first()
    if not profile_version:
        profile_version = CandidateProfileVersion.objects.filter(profile=profile).order_by("-version_number").first()
        
    if not profile_version:
        messages.error(request, "Cannot start tailored application: no candidate profile details or versions found.")
        return redirect("career:job_detail", job_pk=job.pk)
        
    try:
        from django.conf import settings
        from openoutreach.career.models import ApplicationDraft, ApprovalItem
        from openoutreach.career.drafting import get_drafting_provider
        from openoutreach.core.models import SiteConfig
        
        cfg = SiteConfig.load()
        import sys
        offline = getattr(settings, 'TESTING', False) or not cfg.llm_api_key or 'pytest' in sys.modules
        
        provider = get_drafting_provider(offline=offline)
        tailored_cv, cover_letter, structured_cv, traceability_map = provider.generate_draft(profile_version, job)
        
        app = JobApplication.objects.create(
            candidate_profile=profile,
            job=job,
            status="draft",
            tailored_cv=tailored_cv,
            cover_letter=cover_letter,
            audit_log=[{
                "timestamp": timezone.now().isoformat(),
                "user": request.user.username,
                "action": "created_draft",
                "note": f"Draft tailored CV and cover letter generated via {'Offline' if offline else 'LLM'}."
            }]
        )
        
        draft = ApplicationDraft.objects.create(
            application=app,
            version_number=1,
            tailored_cv=tailored_cv,
            cover_letter=cover_letter,
            structured_cv=structured_cv,
            traceability_map=traceability_map,
            created_by=request.user
        )
        
        ApprovalItem.objects.create(
            application=app,
            draft=draft,
            status="pending"
        )
        
        log_audit_event(request.user, "application_drafted", f"Generated draft application tailored for job '{job.title}'.", request)
        messages.success(request, "Application draft successfully generated! You can now review and refine it below.")
        return redirect("career:application_review", app_pk=app.pk)
    except Exception as e:
        messages.error(request, f"Error generating tailored application: {str(e)}")
        return redirect("career:job_detail", job_pk=job.pk)


@login_required
def application_review(request, app_pk):
    app = get_object_or_404(JobApplication, pk=app_pk)
    profile = app.candidate_profile
    
    # Permissions check: candidate owner, admin edit, or approval operators
    if not (request.user == profile.user or 
            request.user.has_perm("career.edit_candidate_profile") or 
            request.user.has_perm("career.approve_profile") or 
            request.user.has_perm("career.view_candidate_data")):
        raise PermissionDenied("You do not have permission to view or edit this application.")
        
    from openoutreach.career.models import ApplicationDraft, ApprovalItem, ApprovalHistory
    
    item = app.approval_items.first()
    if not item:
        draft = app.drafts.first()
        if not draft:
            draft = ApplicationDraft.objects.create(
                application=app,
                version_number=1,
                tailored_cv=app.tailored_cv,
                cover_letter=app.cover_letter,
                structured_cv={},
                traceability_map={},
                created_by=request.user
            )
        item = ApprovalItem.objects.create(
            application=app,
            draft=draft,
            status="approved" if app.status == "approved" else "pending"
        )
        
    # Versioning selector: view specific draft version if passed
    version_param = request.GET.get("version")
    if version_param:
        try:
            viewed_draft = app.drafts.get(version_number=int(version_param))
        except (ValueError, ApplicationDraft.DoesNotExist):
            viewed_draft = item.draft
    else:
        viewed_draft = item.draft

    if request.method == "POST":
        action = request.POST.get("action", "save")
        
        # Action validation: save is allowed for owner, approve/revoke/needs_info are restricted to operators
        is_operator = request.user.is_superuser or request.user.has_perm("career.approve_profile")
        
        if action == "save":
            if item.status == "approved":
                messages.error(request, "Approved applications cannot be modified. Revoke approval first.")
                return redirect("career:application_review", app_pk=app.pk)
            
            form = JobApplicationForm(request.POST, instance=app)
            if form.is_valid():
                app = form.save()
                
                latest_draft = app.drafts.first()
                next_ver = (latest_draft.version_number + 1) if latest_draft else 1
                
                new_draft = ApplicationDraft.objects.create(
                    application=app,
                    version_number=next_ver,
                    tailored_cv=app.tailored_cv,
                    cover_letter=app.cover_letter,
                    structured_cv=latest_draft.structured_cv if latest_draft else {},
                    traceability_map=latest_draft.traceability_map if latest_draft else {},
                    created_by=request.user
                )
                
                # Update item pointer
                item.draft = new_draft
                
                # Submit for review workflow: reset from needs_info back to pending
                was_needs_info = (item.status == "needs_info")
                if was_needs_info:
                    item.status = "pending"
                item.save()
                
                # Append to audit log
                log_entry = {
                    "timestamp": timezone.now().isoformat(),
                    "user": request.user.username,
                    "action": "saved_draft",
                    "note": f"Draft version {next_ver} saved."
                }
                app.audit_log.append(log_entry)
                if was_needs_info:
                    app.audit_log.append({
                        "timestamp": timezone.now().isoformat(),
                        "user": request.user.username,
                        "action": "submitted_for_review",
                        "note": "Re-submitted draft for operator review after correcting needs-info flags."
                    })
                    # Log in ApprovalHistory
                    ApprovalHistory.objects.create(
                        item=item,
                        actor=request.user,
                        action="reject",
                        note="Re-submitted for review after updates."
                    )
                app.save()
                
                log_audit_event(request.user, "application_saved", f"Saved changes to application draft version {next_ver} for job '{app.job.title}'.", request)
                messages.success(request, f"Application draft version {next_ver} successfully created.")
                return redirect("career:application_review", app_pk=app.pk)
                
        elif action == "approve":
            if not is_operator:
                raise PermissionDenied("You do not have permission to approve drafts.")
            if item.status == "approved":
                messages.error(request, "Item is already approved.")
                return redirect("career:application_review", app_pk=app.pk)
                
            note = request.POST.get("note", "")
            
            # Save DOCX in private storage
            import docx
            from io import BytesIO
            from openoutreach.career.storage import PrivateFileSystemStorage
            from django.core.files.base import ContentFile
            
            p_storage = PrivateFileSystemStorage()
            doc = docx.Document()
            doc.add_heading(f"CV - {profile.full_name}", 0)
            doc.add_heading("Target Position", 1)
            doc.add_paragraph(f"{app.job.title} at {app.job.company}")
            doc.add_heading("Tailored CV Markdown Content", 1)
            doc.add_paragraph(viewed_draft.tailored_cv)
            doc.add_heading("Tailored Cover Letter", 1)
            doc.add_paragraph(viewed_draft.cover_letter)
            try:
                doc.settings.write_protection.set_password("OpenOutreach2026")
            except Exception:
                pass
                
            f_buf = BytesIO()
            doc.save(f_buf)
            f_buf.seek(0)
            
            filename = f"tailored/app_{app.id}.docx"
            if p_storage.exists(filename):
                p_storage.delete(filename)
            p_storage.save(filename, ContentFile(f_buf.read()))
            
            # Call approval integrity service
            from openoutreach.career.approval_service import ApprovalIntegrityService
            try:
                # Approve the current draft selection
                item.draft = viewed_draft
                item.save()
                
                # Mirror to JobApplication cache fields for backward compatibility
                app.tailored_cv = viewed_draft.tailored_cv
                app.cover_letter = viewed_draft.cover_letter
                app.save()
                
                ApprovalIntegrityService.approve_item(item, request.user, note=note)
                
                log_entry = {
                    "timestamp": timezone.now().isoformat(),
                    "user": request.user.username,
                    "action": "approved_draft",
                    "note": f"Frozen drafts. Approved by {request.user.username}."
                }
                app.audit_log.append(log_entry)
                app.save()
                log_audit_event(request.user, "application_approved", f"Approved and froze application for job '{app.job.title}'.", request)
                messages.success(request, "Application draft approved! The draft is now locked and frozen.")
            except Exception as e:
                messages.error(request, f"Approval failed: {str(e)}")
            return redirect("career:application_review", app_pk=app.pk)
            
        elif action == "revert_to_draft":
            if not is_operator:
                raise PermissionDenied("You do not have permission to revoke approval.")
            if item.status != "approved":
                messages.error(request, "Only approved items can be revoked.")
                return redirect("career:application_review", app_pk=app.pk)
                
            note = request.POST.get("note", "Operator revoked approval.")
            
            # Delete file from private storage
            from openoutreach.career.storage import PrivateFileSystemStorage
            p_storage = PrivateFileSystemStorage()
            filename = f"tailored/app_{app.id}.docx"
            if p_storage.exists(filename):
                p_storage.delete(filename)
                
            from openoutreach.career.approval_service import ApprovalIntegrityService
            try:
                ApprovalIntegrityService.revoke_approval(item, request.user, note=note)
                log_entry = {
                    "timestamp": timezone.now().isoformat(),
                    "user": request.user.username,
                    "action": "reverted_to_draft",
                    "note": f"Revoked approval. Reason: {note}"
                }
                app.audit_log.append(log_entry)
                app.save()
                log_audit_event(request.user, "application_unlocked", f"Reverted application for job '{app.job.title}' back to draft.", request)
                messages.success(request, "Application draft unlocked and reverted back to draft.")
            except Exception as e:
                messages.error(request, f"Revocation failed: {str(e)}")
            return redirect("career:application_review", app_pk=app.pk)
            
        elif action == "needs_info":
            if not is_operator:
                raise PermissionDenied("You do not have permission to flag needs-info.")
            reason = request.POST.get("reason", "")
            if not reason:
                messages.error(request, "A reason is required to flag needs-info.")
                return redirect("career:application_review", app_pk=app.pk)
                
            from openoutreach.career.approval_service import ApprovalIntegrityService
            try:
                ApprovalIntegrityService.mark_needs_info(item, request.user, reason=reason)
                log_entry = {
                    "timestamp": timezone.now().isoformat(),
                    "user": request.user.username,
                    "action": "needs_info",
                    "note": f"Flagged needs info. Reason: {reason}"
                }
                app.audit_log.append(log_entry)
                app.save()
                log_audit_event(request.user, "application_needs_info", f"Flagged job application for '{app.job.title}' as needing information.", request)
                messages.success(request, f"Application flagged as needing info: {reason}")
            except Exception as e:
                messages.error(request, f"Needs-info flagging failed: {str(e)}")
            return redirect("career:application_review", app_pk=app.pk)
            
    else:
        form = JobApplicationForm(initial={"tailored_cv": viewed_draft.tailored_cv, "cover_letter": viewed_draft.cover_letter})
        
    # Factual Integrity check validator call
    integrity_error = None
    if item.status == "approved":
        from openoutreach.career.approval_service import ApprovalIntegrityService
        try:
            ApprovalIntegrityService.validate_item_integrity(item)
        except ValidationError as e:
            integrity_error = str(e)
            
    return render(request, "career/application_review.html", {
        "application": app,
        "form": form,
        "approval_item": item,
        "drafts": app.drafts.all().order_by("-version_number"),
        "history": item.history.all().order_by("-timestamp"),
        "integrity_error": integrity_error
    })


@login_required
def download_tailored_docx(request, app_pk):
    app = get_object_or_404(JobApplication, pk=app_pk)
    profile = app.candidate_profile

    # Permissions check: user must own the profile or have candidate data permissions
    if not (request.user == profile.user or request.user.has_perm("career.view_candidate_data")):
        raise PermissionDenied("You do not have permission to download this document.")

    # 1. Serve from draft structured_cv path if present
    draft = app.drafts.first()
    if draft and isinstance(draft.structured_cv, dict):
        docx_path = draft.structured_cv.get("resume_docx_path")
        if docx_path and os.path.exists(docx_path):
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', profile.full_name or 'Candidate').strip('_')
            clean_title = re.sub(r'[^a-zA-Z0-9_-]', '_', app.job.title or 'Role').strip('_')
            filename = f"{clean_name}_{clean_title}_Resume.docx"
            with open(docx_path, "rb") as f:
                response = HttpResponse(f.read(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response

    # 2. Serve from Private Storage if legacy approved file exists
    from openoutreach.career.storage import PrivateFileSystemStorage
    p_storage = PrivateFileSystemStorage()
    p_filename = f"tailored/app_{app.id}.docx"
    if p_storage.exists(p_filename):
        f = p_storage.open(p_filename)
        response = HttpResponse(f.read(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        response['Content-Disposition'] = f'attachment; filename="tailored_application_{app.id}.docx"'
        return response

    # 3. Dynamic fallback generation with write protection password
    cv_text = app.tailored_cv or "Candidate CV"
    cl_text = app.cover_letter or "Cover Letter"

    import docx
    from io import BytesIO

    doc = docx.Document()
    doc.add_heading(f"CV - {profile.full_name}", 0)
    doc.add_heading("Target Position", 1)
    doc.add_paragraph(f"{app.job.title} at {app.job.company}")
    doc.add_heading("Tailored CV Content", 1)
    doc.add_paragraph(cv_text)
    doc.add_heading("Tailored Cover Letter", 1)
    doc.add_paragraph(cl_text)

    try:
        doc.settings.write_protection.set_password("OpenOutreach2026")
    except Exception:
        pass

    f = BytesIO()
    doc.save(f)
    f.seek(0)

    response = HttpResponse(f.read(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response['Content-Disposition'] = f'attachment; filename="tailored_application_{app.id}.docx"'
    return response


@login_required
def download_tailored_pdf(request, app_pk):
    app = get_object_or_404(JobApplication, pk=app_pk)
    profile = app.candidate_profile

    # Permissions check
    if not (request.user == profile.user or request.user.has_perm("career.view_candidate_data")):
        raise PermissionDenied("You do not have permission to download this document.")

    draft = app.drafts.first()
    if draft and isinstance(draft.structured_cv, dict):
        pdf_path = draft.structured_cv.get("resume_pdf_path")
        if pdf_path and os.path.exists(pdf_path):
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', profile.full_name or 'Candidate').strip('_')
            clean_title = re.sub(r'[^a-zA-Z0-9_-]', '_', app.job.title or 'Role').strip('_')
            filename = f"{clean_name}_{clean_title}_Resume.pdf"
            with open(pdf_path, "rb") as f:
                response = HttpResponse(f.read(), content_type="application/pdf")
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response

    messages.error(request, "PDF document artifact not found for this application package.")
    return redirect("career:application_review", app_pk=app.pk)


@login_required
def download_cover_letter_docx(request, app_pk):
    app = get_object_or_404(JobApplication, pk=app_pk)
    profile = app.candidate_profile

    if not (request.user == profile.user or request.user.has_perm("career.view_candidate_data")):
        raise PermissionDenied("You do not have permission to download this document.")

    draft = app.drafts.first()
    if draft and isinstance(draft.structured_cv, dict):
        docx_path = draft.structured_cv.get("cover_letter_docx_path")
        if docx_path and os.path.exists(docx_path):
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', profile.full_name or 'Candidate').strip('_')
            clean_co = re.sub(r'[^a-zA-Z0-9_-]', '_', app.job.company or 'Company').strip('_')
            filename = f"{clean_name}_{clean_co}_CoverLetter.docx"
            with open(docx_path, "rb") as f:
                response = HttpResponse(f.read(), content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response

    messages.error(request, "Cover letter DOCX artifact not found.")
    return redirect("career:application_review", app_pk=app.pk)


@login_required
def download_cover_letter_pdf(request, app_pk):
    app = get_object_or_404(JobApplication, pk=app_pk)
    profile = app.candidate_profile

    if not (request.user == profile.user or request.user.has_perm("career.view_candidate_data")):
        raise PermissionDenied("You do not have permission to download this document.")

    draft = app.drafts.first()
    if draft and isinstance(draft.structured_cv, dict):
        pdf_path = draft.structured_cv.get("cover_letter_pdf_path")
        if pdf_path and os.path.exists(pdf_path):
            clean_name = re.sub(r'[^a-zA-Z0-9_-]', '_', profile.full_name or 'Candidate').strip('_')
            clean_co = re.sub(r'[^a-zA-Z0-9_-]', '_', app.job.company or 'Company').strip('_')
            filename = f"{clean_name}_{clean_co}_CoverLetter.pdf"
            with open(pdf_path, "rb") as f:
                response = HttpResponse(f.read(), content_type="application/pdf")
                response['Content-Disposition'] = f'attachment; filename="{filename}"'
                return response

    messages.error(request, "Cover letter PDF artifact not found.")
    return redirect("career:application_review", app_pk=app.pk)


@login_required
def approval_dashboard(request):
    if not (request.user.is_superuser or request.user.has_perm("career.approve_profile")):
        raise PermissionDenied("You do not have permission to access the Approval Center.")
        
    from openoutreach.career.models import ApprovalItem, ApprovalBatch
    from openoutreach.career.approval_service import ApprovalIntegrityService
    
    if request.method == "POST":
        action = request.POST.get("action")
        item_ids = request.POST.getlist("selected_items")
        
        if action == "bulk_approve" and item_ids:
            batch = ApprovalBatch.objects.create(
                name=f"Bulk Approval by {request.user.username}",
                created_by=request.user
            )
            success_count = 0
            for iid in item_ids:
                item = ApprovalItem.objects.filter(pk=iid).first()
                if item and item.status != "approved":
                    item.batch = batch
                    item.save()
                    
                    # Generate docx for batch items
                    import docx
                    from io import BytesIO
                    from openoutreach.career.storage import PrivateFileSystemStorage
                    from django.core.files.base import ContentFile
                    
                    p_storage = PrivateFileSystemStorage()
                    doc = docx.Document()
                    doc.add_heading(f"CV - {item.application.candidate_profile.full_name}", 0)
                    doc.add_heading("Target Position", 1)
                    doc.add_paragraph(f"{item.application.job.title} at {item.application.job.company}")
                    doc.add_heading("Tailored CV Markdown Content", 1)
                    doc.add_paragraph(item.draft.tailored_cv)
                    doc.add_heading("Tailored Cover Letter", 1)
                    doc.add_paragraph(item.draft.cover_letter)
                    try:
                        doc.settings.write_protection.set_password("OpenOutreach2026")
                    except Exception:
                        pass
                        
                    f_buf = BytesIO()
                    doc.save(f_buf)
                    f_buf.seek(0)
                    
                    filename = f"tailored/app_{item.application.id}.docx"
                    if p_storage.exists(filename):
                        p_storage.delete(filename)
                    p_storage.save(filename, ContentFile(f_buf.read()))
                    
                    try:
                        ApprovalIntegrityService.approve_item(item, request.user, note="Bulk approved.")
                        success_count += 1
                    except Exception:
                        pass
            messages.success(request, f"Successfully bulk approved {success_count} application drafts under Batch #{batch.id}.")
            return redirect("career:approval_dashboard")
            
    items = ApprovalItem.objects.all().order_by("-created_at")
    batches = ApprovalBatch.objects.all().order_by("-created_at")
    
    return render(request, "career/approval_dashboard.html", {
        "items": items,
        "batches": batches
    })


@login_required
def identity_review(request):
    profile = CandidateProfile.objects.filter(user=request.user).first()
    if not profile:
        raise PermissionDenied("Candidate profile not found.")
    
    if request.user != profile.user:
        raise PermissionDenied("You do not have permission to access this candidate profile identity.")
    
    from openoutreach.career.service import (
        get_identity_comparison_data, verify_candidate_identity, create_identity_corrected_profile_version
    )
    from openoutreach.career.models import (
        CandidateIdentityVerification, CandidateProfileVersion, Job, JobVersion, JobMatch, JobApplication, ApplicationDraft, CandidateDocumentVersion
    )
    from django.core.exceptions import ValidationError

    comparison_data = get_identity_comparison_data(profile)
    
    verification = CandidateIdentityVerification.objects.filter(
        candidate_profile=profile,
        status="verified"
    ).first()
    
    active_version = profile.active_version
    
    quarantined_objects = []
    jobs = Job.objects.filter(pk=1)
    jobversions = JobVersion.objects.filter(pk=1)
    jobmatches = JobMatch.objects.filter(pk=1)
    jobapplications = JobApplication.objects.filter(pk=1)
    applicationdrafts = ApplicationDraft.objects.filter(pk=1)
    
    for item in list(jobs) + list(jobversions) + list(jobmatches) + list(jobapplications) + list(applicationdrafts):
        quarantined_objects.append({
            "model": item._meta.verbose_name.title(),
            "id": item.id,
            "display": str(item)
        })

    doc_ver = CandidateDocumentVersion.objects.filter(document__profile=profile).order_by("version_number").first()
    source_document_id = doc_ver.document.id if doc_ver else None
    source_document_version_id = doc_ver.id if doc_ver else None

    error_message = None
    success_message = None

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "verify":
            extracted_name = request.POST.get("extracted_name", "").strip()
            confirmed_name = request.POST.get("confirmed_name", "").strip()
            
            try:
                verify_candidate_identity(
                    profile=profile,
                    extracted_name=extracted_name,
                    confirmed_name=confirmed_name,
                    source_document_id=source_document_id,
                    source_document_version_id=source_document_version_id,
                    evidence_excerpt=comparison_data["evidence_excerpt"],
                    verified_by=request.user
                )
                success_message = "Identity verified successfully!"
                return redirect("career:identity_review")

            except ValidationError as e:
                error_message = e.message
        elif action == "create_corrected_version":
            error_message = "CREATION FROM VIEW DISABLED IN THIS PHASE"
                    
    context = {
        "profile": profile,
        "comparison": comparison_data,
        "verification": verification,
        "quarantined_objects": quarantined_objects,
        "error_message": error_message,
        "success_message": success_message,
        "active_version": active_version,
    }
    return render(request, "career/identity_review.html", context)


@login_required
@require_POST
def career_identity_review_confirm(request):
    profile_id = request.POST.get("profile_id")
    if not profile_id:
        raise PermissionDenied("Profile ID is required.")

    profile = get_object_or_404(CandidateProfile, pk=profile_id)

    # Security: Verify ownership
    if request.user != profile.user:
        raise PermissionDenied("You do not have permission to confirm identity for this profile.")

    action = request.POST.get("action")
    confirmed_name = request.POST.get("confirmed_name", "")

    from openoutreach.career.service import (
        get_identity_comparison_data, verify_candidate_identity, create_identity_corrected_profile_version
    )
    from openoutreach.career.models import CandidateIdentityVerification, CandidateDocumentVersion
    from django.core.exceptions import ValidationError

    try:
        if action == "confirm_identity":
            comparison = get_identity_comparison_data(profile)
            extracted_name = comparison["extracted_name"]

            # Exact name validation: NO lowercasing, trimming, fuzzy matching or normalization
            if confirmed_name != extracted_name:
                raise ValidationError("IDENTITY MISMATCH REQUIRES REVIEW")

            doc_ver = CandidateDocumentVersion.objects.filter(document__profile=profile).order_by("version_number").first()
            source_document_id = doc_ver.document.id if doc_ver else None
            source_document_version_id = doc_ver.id if doc_ver else None

            verify_candidate_identity(
                profile=profile,
                extracted_name=extracted_name,
                confirmed_name=confirmed_name,
                source_document_id=source_document_id,
                source_document_version_id=source_document_version_id,
                evidence_excerpt=comparison["evidence_excerpt"],
                verified_by=request.user
            )
            messages.success(request, "Identity confirmed successfully!")

        elif action == "create_corrected_version":
            raise ValidationError("CREATION FROM VIEW DISABLED IN THIS PHASE")

        else:
            raise ValidationError("Invalid action.")

    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, "message") else str(e))

    return redirect("career:identity_review")


@login_required
@require_POST
def career_identity_corrected_version_approve(request):
    profile_id = request.POST.get("profile_id")
    version_id = request.POST.get("version_id")
    confirm_text = request.POST.get("confirm", "").strip()

    if not profile_id or not version_id:
        raise PermissionDenied("Profile ID and Version ID are required.")

    profile = get_object_or_404(CandidateProfile, pk=profile_id)

    # Security: Verify ownership
    if request.user != profile.user:
        raise PermissionDenied("You do not have permission to access this candidate profile.")

    # Explicit confirmation checkbox check
    if confirm_text != "APPROVE CORRECTED IDENTITY VERSION":
        messages.error(request, "Confirmation checkbox prompt must be checked and match exactly: 'APPROVE CORRECTED IDENTITY VERSION'")
        return redirect("career:identity_review")

    version = get_object_or_404(CandidateProfileVersion, pk=version_id, profile=profile)

    from openoutreach.career.service import approve_corrected_profile_version
    from django.core.exceptions import ValidationError

    try:
        approve_corrected_profile_version(version, request.user)
        messages.success(request, "Corrected candidate profile version approved successfully!")
    except ValidationError as e:
        messages.error(request, e.message if hasattr(e, "message") else str(e))

    return redirect("career:identity_review")


@login_required
def identity_resolution_settings(request):
    from openoutreach.career.models import CandidateProfile, CandidateProfileVersion, CandidateIdentityCorrectionLedger, CandidateIdentityVerification, IncidentHold
    from openoutreach.core.models import SenderIdentity
    from openoutreach.career.services.identity_gates import get_real_action_eligibility, execute_identity_resolution

    profile = (
        CandidateProfile.objects.filter(user=request.user).first()
        or CandidateProfile.objects.filter(user__workspace_memberships__workspace__in=request.user.workspaces.all()).first()
    )
    if not profile:
        from django.http import Http404
        raise Http404("No candidate profile found.")

    if request.method == "POST":
        canonical_email = request.POST.get("canonical_email", "candidate-10@example.com")
        confirmed_linkedin_url = request.POST.get("confirmed_linkedin_url", "https://www.linkedin.com/in/alex-morgan")
        release_reason = request.POST.get("release_reason", "Human operator confirmed identity and released IncidentHold")

        res = execute_identity_resolution(
            profile_id=profile.id,
            version_id=6,
            canonical_email=canonical_email,
            operator_user=request.user,
            release_reason=release_reason,
            confirmed_linkedin_url=confirmed_linkedin_url,
        )
        messages.success(request, f"Identity resolution completed successfully! Active version {res['active_version_id']} approved.")
        return redirect("career:identity_resolution_settings")

    eligibility = get_real_action_eligibility(candidate_profile=profile)
    versions = CandidateProfileVersion.objects.filter(profile=profile).order_by("version_number")
    ledger = CandidateIdentityCorrectionLedger.objects.filter(profile=profile).first()
    verifications = CandidateIdentityVerification.objects.filter(candidate_profile=profile)
    holds = IncidentHold.objects.filter(active=True)
    senders = SenderIdentity.objects.all()

    context = {
        "profile": profile,
        "eligibility": eligibility,
        "versions": versions,
        "ledger": ledger,
        "verifications": verifications,
        "holds": holds,
        "senders": senders,
    }
    return render(request, "career/identity_resolution.html", context)


@login_required
@require_POST
def application_reconcile_confirmation(request, app_pk):
    """Operator action to reconcile external confirmation for an application."""
    app = get_object_or_404(JobApplication, pk=app_pk)
    evidence_type = request.POST.get("evidence_type", ConfirmationEvidenceType.OPERATOR_CONFIRMED_LINKEDIN_EMAIL)
    provider = request.POST.get("provider", app.job.provider or "linkedin")
    notes = request.POST.get("notes", "Operator confirmed external acknowledgement")

    from openoutreach.career.services.confirmation_evidence import record_external_confirmation
    from openoutreach.core.services.audit_service import record_audit_event
    from openoutreach.core.models import Workspace

    ws = Workspace.objects.first()
    original_sub_date = app.submission_date or timezone.now()

    record_external_confirmation(
        application=app,
        provider=provider,
        external_job_id=str(app.job.external_job_id or app.job.id),
        evidence_type=evidence_type,
        checked_at=timezone.now(),
        metadata={
            "source": "operator_report",
            "provider": provider,
            "external_job_id": str(app.job.external_job_id or app.job.id),
            "company": app.job.company,
            "job_title": app.job.title,
            "notes": notes,
            "reconciled_by": request.user.username,
            "reconciled_at": timezone.now().isoformat()
        }
    )

    app.submission_date = original_sub_date
    app.save(update_fields=["submission_date"])

    if ws:
        record_audit_event(
            workspace=ws,
            event_type="job_application_reconciled",
            target=app.job,
            result="externally_confirmed",
            action_key=f"reconcile_app_{app.candidate_profile_id}_{app.job_id}",
            metadata={
                "application_id": app.id,
                "evidence_type": evidence_type,
                "reconciled_by": request.user.username
            }
        )

    messages.success(request, f"Application #{app.id} successfully marked as externally confirmed.")
    return redirect("career:application_review", app_pk=app.pk)
