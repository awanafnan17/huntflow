import os
import zipfile
import logging
from django import forms
from django.core.exceptions import ValidationError
from openoutreach.career.models import CandidateProfile, CandidateDocument, Job, JobApplication

logger = logging.getLogger(__name__)

# Max upload size: 5MB
MAX_UPLOAD_SIZE = 5 * 1024 * 1024

class CandidateProfileForm(forms.ModelForm):
    class Meta:
        model = CandidateProfile
        fields = ["full_name", "email", "phone", "linkedin_url", "summary"]
        widgets = {
            "full_name": forms.TextInput(attrs={"class": "input-text"}),
            "email": forms.EmailInput(attrs={"class": "input-text"}),
            "phone": forms.TextInput(attrs={"class": "input-text"}),
            "linkedin_url": forms.URLInput(attrs={"class": "input-text"}),
            "summary": forms.Textarea(attrs={"class": "input-text", "rows": 4}),
        }

class DocumentUploadForm(forms.Form):
    DOCUMENT_TYPES = [
        ("cv", "CV / Resume"),
        ("cover_letter", "Cover Letter"),
        ("other", "Other"),
    ]
    name = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"class": "input-text"}))
    document_type = forms.ChoiceField(choices=DOCUMENT_TYPES, widget=forms.Select(attrs={"class": "select-box"}))
    file = forms.FileField(widget=forms.ClearableFileInput(attrs={"class": "input-text"}))

    def clean_file(self):
        uploaded_file = self.cleaned_data.get("file")
        if not uploaded_file:
            raise ValidationError("No file was uploaded.")

        filename = uploaded_file.name.lower()
        ext = os.path.splitext(filename)[1]

        # 1. Non-empty file validation
        if uploaded_file.size == 0:
            raise ValidationError("The uploaded file is empty (0 bytes).")

        # 2. Maximum size validation
        if uploaded_file.size > MAX_UPLOAD_SIZE:
            raise ValidationError(f"File size exceeds the maximum limit of 5MB (size: {uploaded_file.size / 1024 / 1024:.2f}MB).")

        # 3. Allowed extensions check
        allowed_extensions = [".pdf", ".docx", ".txt", ".md"]
        if ext not in allowed_extensions:
            raise ValidationError(f"File extension '{ext}' is not supported and not allowed. Only PDF, DOCX, TXT, and MD are allowed.")

        # 4. MIME type and signature verification
        content_type = uploaded_file.content_type
        allowed_mimes = [
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "text/plain",
            "text/markdown"
        ]
        if content_type not in allowed_mimes:
            raise ValidationError(f"File type '{content_type}' is not allowed.")

        # Read first bytes for signature check
        try:
            pos = uploaded_file.tell()
            uploaded_file.seek(0)
            header = uploaded_file.read(5)
            uploaded_file.seek(pos)
        except Exception as e:
            raise ValidationError(f"Could not read file header: {str(e)}")

        if ext == ".pdf":
            if content_type != "application/pdf":
                raise ValidationError("MIME type mismatch for PDF file.")
            # PDF magic bytes: %PDF-
            if not header.startswith(b"%PDF-"):
                raise ValidationError("Invalid PDF file. The file header signature is incorrect.")
        elif ext == ".docx":
            if content_type != "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
                raise ValidationError("MIME type mismatch for DOCX file.")
            # ZIP/DOCX magic bytes: PK\x03\x04
            if not header.startswith(b"PK\x03\x04"):
                raise ValidationError("Invalid DOCX file. The file header signature is incorrect.")
            
            # Verify ZIP structure
            try:
                # We need to seek back to start to parse with zipfile
                pos = uploaded_file.tell()
                uploaded_file.seek(0)
                # Read into a bytes buffer
                buf = io.BytesIO(uploaded_file.read())
                uploaded_file.seek(pos)
                
                with zipfile.ZipFile(buf) as zf:
                    namelist = zf.namelist()
                    # Check for DOCX structure files
                    if "[Content_Types].xml" not in namelist:
                        raise ValidationError("Invalid DOCX file structure: [Content_Types].xml is missing.")
                    if "word/document.xml" not in namelist:
                        raise ValidationError("Invalid DOCX file structure: word/document.xml is missing. This might be a generic ZIP archive.")
            except zipfile.BadZipFile:
                raise ValidationError("Invalid DOCX file. The archive is corrupted or not a valid ZIP container.")
            except Exception as e:
                if isinstance(e, ValidationError):
                    raise e
                raise ValidationError(f"Error validating DOCX file structure: {str(e)}")

        # 5. Malware scanning integration point
        logger.info(f"Simulating malware scanning check for file: {uploaded_file.name}")

        return uploaded_file


class RawCVForm(forms.Form):
    raw_text = forms.CharField(
        widget=forms.Textarea(attrs={
            "class": "input-text",
            "rows": 15,
            "placeholder": "Paste your raw CV text here (max 50,000 characters)..."
        }),
        max_length=50000,
        help_text="Maximum 50,000 characters."
    )

    def clean_raw_text(self):
        text = self.cleaned_data.get("raw_text", "").strip()
        if not text:
            raise ValidationError("Paste text cannot be empty.")
        # Rejects scripts and raw HTML content
        if "<script" in text.lower() or "href=" in text.lower() or "javascript:" in text.lower():
            raise ValidationError("Executable payloads, script tags, or HTML content are not allowed.")
        return text


class JobForm(forms.ModelForm):
    class Meta:
        model = Job
        fields = ["title", "company", "location", "description", "job_url", "source"]
        widgets = {
            "title": forms.TextInput(attrs={"class": "input-text"}),
            "company": forms.TextInput(attrs={"class": "input-text"}),
            "location": forms.TextInput(attrs={"class": "input-text"}),
            "description": forms.Textarea(attrs={"class": "input-text", "rows": 8}),
            "job_url": forms.URLInput(attrs={"class": "input-text"}),
            "source": forms.TextInput(attrs={"class": "input-text"}),
        }


class JobApplicationForm(forms.ModelForm):
    class Meta:
        model = JobApplication
        fields = ["tailored_cv", "cover_letter"]
        widgets = {
            "tailored_cv": forms.Textarea(attrs={"class": "input-text", "rows": 15, "style": "font-family: monospace;"}),
            "cover_letter": forms.Textarea(attrs={"class": "input-text", "rows": 15, "style": "font-family: monospace;"}),
        }


class JobSearchPreferenceForm(forms.ModelForm):
    keywords_text = forms.CharField(
        required=False,
        label="Search Keywords",
        widget=forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. Python, Django, Remote Senior Engineer"}),
        help_text="Comma-separated keywords applied on LinkedIn"
    )
    job_titles_text = forms.CharField(
        required=False,
        label="Target Job Titles",
        widget=forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. Senior Software Engineer, Backend Lead"}),
        help_text="Comma-separated job titles applied on LinkedIn"
    )
    locations_text = forms.CharField(
        required=False,
        label="Locations",
        widget=forms.TextInput(attrs={"class": "input-text", "placeholder": "e.g. San Francisco CA, Remote, United States"}),
        help_text="Comma-separated target locations"
    )

    class Meta:
        from openoutreach.career.models import JobSearchPreference
        model = JobSearchPreference
        fields = [
            "date_posted",
            "easy_apply_only",
            "minimum_salary",
            "currency",
            "work_authorization_required",
            "visa_sponsorship_required",
            "match_threshold",
            "execution_mode",
        ]
        widgets = {
            "match_threshold": forms.NumberInput(attrs={
                "type": "range",
                "min": "0",
                "max": "100",
                "step": "1",
                "id": "id_match_threshold",
                "oninput": "document.getElementById('threshold_val_display').innerText = this.value + '%';"
            }),
            "execution_mode": forms.Select(attrs={"class": "select-box"}),
            "date_posted": forms.Select(choices=[
                ("any_time", "Any Time"),
                ("past_24_hours", "Past 24 Hours"),
                ("past_week", "Past Week"),
                ("past_month", "Past Month"),
            ], attrs={"class": "select-box"}),
        }

    def clean_match_threshold(self):
        val = self.cleaned_data.get("match_threshold")
        if val is None or val < 0 or val > 100:
            raise ValidationError("Match threshold must be between 0% and 100%.")
        return val


