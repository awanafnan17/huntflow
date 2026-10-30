from django.urls import path
from openoutreach.career import views

app_name = "career"

urlpatterns = [
    path("", views.career_overview, name="overview"),
    path("profile/", views.career_overview, name="profile"),
    path("documents/", views.career_overview, name="documents"),
    path("profile/versions/", views.career_overview, name="profile_versions"),
    path("audit/", views.audit_trail, name="audit"),
    
    path("edit/", views.profile_edit, name="profile_edit"),
    path("upload/", views.document_upload, name="document_upload"),
    path("download/<int:version_pk>/", views.document_download, name="document_download"),
    path("approve/<int:version_pk>/", views.profile_approve, name="profile_approve"),
    path("document/<int:document_pk>/archive/", views.document_archive, name="document_archive"),
    
    path("raw_text/", views.raw_cv_input, name="raw_cv_input"),
    path("document/<int:version_pk>/parse/", views.document_parse, name="document_parse"),
    path("extraction/<int:record_pk>/review/", views.extraction_review, name="extraction_review"),
    
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/add/", views.job_add, name="job_add"),
    path("jobs/<int:job_pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:job_pk>/match/", views.job_match, name="job_match"),
    path("jobs/<int:job_pk>/apply/", views.job_apply, name="job_apply"),
    path("applications/<int:app_pk>/review/", views.application_review, name="application_review"),
    path("applications/<int:app_pk>/reconcile/", views.application_reconcile_confirmation, name="application_reconcile"),
    path("applications/<int:app_pk>/download_docx/", views.download_tailored_docx, name="download_docx"),
    path("applications/<int:app_pk>/download_pdf/", views.download_tailored_pdf, name="download_pdf"),
    path("applications/<int:app_pk>/download_cover_docx/", views.download_cover_letter_docx, name="download_cover_docx"),
    path("applications/<int:app_pk>/download_cover_pdf/", views.download_cover_letter_pdf, name="download_cover_pdf"),
    path("applications/approval_center/", views.approval_dashboard, name="approval_dashboard"),
    path("identity-review/", views.identity_review, name="identity_review"),
    path("identity-review/confirm/", views.career_identity_review_confirm, name="identity_review_confirm"),
    path("identity-review/approve/", views.career_identity_corrected_version_approve, name="identity_review_approve"),
    path("settings/identity-resolution/", views.identity_resolution_settings, name="identity_resolution_settings"),
]
