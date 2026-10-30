import hashlib
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from openoutreach.career.models import (
    JobApplication,
    ApplicationDraft,
    ApprovalBatch,
    ApprovalItem,
    ApprovalHistory,
    ApplicationApproval,
    CandidateFact
)

class ApprovalIntegrityService:
    @staticmethod
    def validate_item_integrity(item: ApprovalItem) -> bool:
        """Validate approval integrity constraints.
        Returns True if approved and valid, False otherwise. Raises ValidationError if integrity is breached.
        """
        if item.status != "approved":
            return False
            
        # 1. Verify ApplicationApproval matches the hashes
        try:
            approval = item.application.approval
        except ApplicationApproval.DoesNotExist:
            raise ValidationError("Approval record missing for approved application.")
            
        cv_text = approval.approved_cv_text
        cl_text = approval.approved_cover_letter_text
        
        cv_hash = hashlib.sha256(cv_text.encode("utf-8")).hexdigest()
        cl_hash = hashlib.sha256(cl_text.encode("utf-8")).hexdigest()
        
        if approval.cv_hash != cv_hash:
            raise ValidationError("Approved CV hash mismatch (content tampered).")
        if approval.cover_letter_hash != cl_hash:
            raise ValidationError("Approved Cover Letter hash mismatch (content tampered).")
            
        # 2. Check factual traceability: verify referenced facts in draft's traceability_map still exist in database
        draft = item.draft
        trace_map = draft.traceability_map or {}
        
        # Experience mappings
        for exp in trace_map.get("experience_mappings", []):
            fact_id = exp.get("fact_id")
            if fact_id and not CandidateFact.objects.filter(id=fact_id).exists():
                raise ValidationError(f"Factual traceability broken: experience fact {fact_id} has been deleted.")
                
        # Skill mappings
        for sk in trace_map.get("skill_mappings", []):
            fact_id = sk.get("fact_id")
            if fact_id and not CandidateFact.objects.filter(id=fact_id).exists():
                raise ValidationError(f"Factual traceability broken: skill fact {fact_id} has been deleted.")
                
        return True

    @staticmethod
    def approve_item(item: ApprovalItem, actor: User, note: str = "") -> ApplicationApproval:
        """Approve and freeze draft contents, creating hash records and immutable log entry."""
        if item.status == "approved":
            raise ValidationError("Item is already approved.")
            
        app = item.application
        draft = item.draft
        
        cv_text = draft.tailored_cv
        cl_text = draft.cover_letter
        
        cv_hash = hashlib.sha256(cv_text.encode("utf-8")).hexdigest()
        cl_hash = hashlib.sha256(cl_text.encode("utf-8")).hexdigest()
        
        # Create or update ApplicationApproval
        approval, created = ApplicationApproval.objects.update_or_create(
            application=app,
            defaults={
                "approved_cv_text": cv_text,
                "approved_cover_letter_text": cl_text,
                "cv_hash": cv_hash,
                "cover_letter_hash": cl_hash,
                "approved_by": actor
            }
        )
        
        # Update statuses
        item.status = "approved"
        item.save()
        
        app.status = "approved"
        app.save()
        
        # Create immutable history entry
        ApprovalHistory.objects.create(
            item=item,
            actor=actor,
            action="approve",
            note=note or f"Approved CV hash: {cv_hash[:10]}, CL hash: {cl_hash[:10]}"
        )
        
        return approval

    @staticmethod
    def revoke_approval(item: ApprovalItem, actor: User, note: str) -> None:
        """Revoke approval, clearing ApplicationApproval and reverting application status to draft."""
        if item.status != "approved":
            raise ValidationError("Only approved items can be revoked.")
            
        app = item.application
        
        # Delete ApplicationApproval record
        if hasattr(app, "approval"):
            app.approval.delete()
            
        item.status = "revoked"
        item.save()
        
        app.status = "draft"
        app.save()
        
        # Create history entry
        ApprovalHistory.objects.create(
            item=item,
            actor=actor,
            action="revoke",
            note=note
        )

    @staticmethod
    def mark_needs_info(item: ApprovalItem, actor: User, reason: str) -> None:
        """Move item to needs_info status specifying the info needed."""
        if not reason:
            raise ValidationError("A reason is required to flag needs-info.")
            
        item.status = "needs_info"
        item.needs_info_reason = reason
        item.save()
        
        # Log history
        ApprovalHistory.objects.create(
            item=item,
            actor=actor,
            action="needs_info",
            note=reason
        )
