# openoutreach/linkedin/browser/easy_apply.py
"""LinkedIn Easy Apply Browser Adapter for OpenOutreach HuntFlow.

Operates LinkedIn's In-App Easy Apply modal workflow using an authenticated
AccountSession. Strictly respects candidate facts and avoids fabricating screening answers.
Provides deterministic, job-specific applied verification.
"""
from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, Optional, Tuple
from django.utils import timezone

logger = logging.getLogger(__name__)


def extract_linkedin_job_id(job_url: str) -> Optional[str]:
    """Extract external numeric job ID from a LinkedIn URL."""
    if not job_url:
        return None
    match = re.search(r'(?:/jobs/view/|currentJobId=)(\d+)', str(job_url))
    if match:
        return match.group(1)
    # Also check trailing numeric path segment
    match_trail = re.search(r'/jobs/(\d+)', str(job_url))
    if match_trail:
        return match_trail.group(1)
    return None


from openoutreach.career.services.confirmation_evidence import (
    VerificationResult,
    VerificationStatus,
    ConfirmationEvidenceType,
)


def verify_linkedin_job_applied(
    session,
    external_job_id: str,
    job_url: Optional[str] = None,
    page=None
) -> VerificationResult:
    """
    Deterministic, job-specific verification of applied status on LinkedIn.
    Never searches global body text for generic words like 'applied'.
    
    Returns:
        VerificationResult(is_applied: bool, evidence_type: str, metadata: dict, status: str)
        Status enum: CONFIRMED_APPLIED, NOT_APPLIED, UNKNOWN
    """
    if not session and not page:
        return VerificationResult(False, "no_session", {}, status=VerificationStatus.UNKNOWN)

    if not page:
        session.ensure_browser()
        page = session.page

    clean_id = str(external_job_id).strip() if external_job_id else extract_linkedin_job_id(job_url or "")
    if not clean_id:
        return VerificationResult(False, "missing_job_id", {}, status=VerificationStatus.UNKNOWN)

    target_url = job_url or f"https://www.linkedin.com/jobs/view/{clean_id}/"

    logger.info("Verifying applied status for LinkedIn Job ID %s at %s", clean_id, target_url)

    from openoutreach.linkedin.services.evidence_cache import record_provider_read
    from openoutreach.linkedin.models import ProviderOperationType

    prof = getattr(session, "linkedin_profile", None) if session else None
    ws = getattr(prof, "workspace", None) if prof else None

    # ─────────────────────────────────────────────────────────────
    # SIGNAL 1: Target Job View Top-Card Action Area
    # ─────────────────────────────────────────────────────────────
    try:
        record_provider_read(
            operation_type=ProviderOperationType.JOB_DETAIL,
            resource_identifier=clean_id,
            cache_hit=False,
            linkedin_profile=prof,
            workspace=ws,
            success=True,
            read_cost=1,
        )
        page.goto(target_url, wait_until="commit", timeout=35000)
        page.wait_for_timeout(3500)

        current_url = page.url
        curr_id = extract_linkedin_job_id(current_url)
        if curr_id and curr_id != clean_id:
            logger.warning("Target job URL redirected to different Job ID: %s != %s", curr_id, clean_id)
            return VerificationResult(False, "job_id_mismatch", {"expected_id": clean_id, "actual_id": curr_id}, status=VerificationStatus.UNKNOWN)

        # Check if an active Apply button still exists in the action container
        apply_btn = page.locator('.jobs-s-apply button, button.jobs-apply-button, .jobs-unified-top-card__actions button:has-text("Apply")').first
        has_active_apply_btn = apply_btn.count() > 0 and apply_btn.is_visible()

        # Check scoped status feedback inside the job's application container
        # Note: Must strictly target .jobs-s-apply or .jobs-unified-top-card__actions, NEVER .jobs-description
        topcard_status = page.locator(
            '.jobs-s-apply .artdeco-inline-feedback--success, '
            '.jobs-s-apply .artdeco-inline-feedback, '
            '.jobs-applied-badge, '
            '.jobs-s-apply .jobs-apply-button--applied, '
            '.jobs-unified-top-card__actions .artdeco-inline-feedback--success'
        )

        for i in range(topcard_status.count()):
            el = topcard_status.nth(i)
            if not el.is_visible():
                continue
            text = el.inner_text().strip().lower()
            # Must strictly match exact applied indicator, not generic sentences
            if text in ["applied", "application submitted", "applied on linkedin"] or text.startswith("applied "):
                if not has_active_apply_btn:
                    logger.info("Signal 1 verified: Exact applied status in top card for Job %s", clean_id)
                    return VerificationResult(
                        True,
                        ConfirmationEvidenceType.LINKEDIN_TOPCARD,
                        {
                            "external_job_id": clean_id,
                            "verification_method": "signal_1_topcard_exact",
                            "status_text": text,
                            "verified_at": timezone.now().isoformat()
                        },
                        status=VerificationStatus.CONFIRMED_APPLIED
                    )

        # If active Apply button is present on the exact target job, it is definitely not applied
        if has_active_apply_btn:
            aria = apply_btn.get_attribute("aria-label") or ""
            btn_txt = apply_btn.inner_text().strip()
            logger.info("Signal 1: Active apply button found for Job %s ('%s' / '%s') -> NOT APPLIED", clean_id, btn_txt, aria)
            return VerificationResult(
                False,
                "not_applied_active_button_present",
                {"external_job_id": clean_id, "button_text": btn_txt},
                status=VerificationStatus.NOT_APPLIED
            )
        # If direct page was indeterminate, attempt inspection via search-detail pane
        if not has_active_apply_btn and "/jobs/search/" not in target_url:
            search_pane_url = f"https://www.linkedin.com/jobs/search/?currentJobId={clean_id}&f_AL=true"
            record_provider_read(
                operation_type=ProviderOperationType.JOB_DETAIL,
                resource_identifier=f"search_{clean_id}",
                cache_hit=False,
                linkedin_profile=prof,
                workspace=ws,
                success=True,
                read_cost=1,
            )
            page.goto(search_pane_url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(3500)
            apply_btn_sp = page.locator('.jobs-details__main-content button.jobs-apply-button, .jobs-search__job-details button.jobs-apply-button, .jobs-s-apply button').first
            if apply_btn_sp.count() > 0 and apply_btn_sp.is_visible():
                btn_txt_sp = apply_btn_sp.inner_text().strip()
                logger.info("Signal 1 (Search Pane fallback): Active apply button found for Job %s ('%s') -> NOT APPLIED", clean_id, btn_txt_sp)
                return VerificationResult(
                    False,
                    "not_applied_active_button_present",
                    {"external_job_id": clean_id, "button_text": btn_txt_sp, "source": "search_pane_fallback"},
                    status=VerificationStatus.NOT_APPLIED
                )
            # Check applied badge in search pane
            sp_applied = page.locator('.jobs-details__main-content .jobs-applied-badge, .jobs-details__main-content .artdeco-inline-feedback--success, .jobs-search__job-details .artdeco-inline-feedback--success').first
            if sp_applied.count() > 0 and sp_applied.is_visible():
                sp_txt = sp_applied.inner_text().strip().lower()
                if "applied" in sp_txt:
                    logger.info("Signal 1 (Search Pane fallback): Applied badge found for Job %s", clean_id)
                    return VerificationResult(
                        True,
                        ConfirmationEvidenceType.LINKEDIN_TOPCARD,
                        {
                            "external_job_id": clean_id,
                            "verification_method": "signal_1_search_pane_fallback",
                            "status_text": sp_txt,
                            "verified_at": timezone.now().isoformat()
                        },
                        status=VerificationStatus.CONFIRMED_APPLIED
                    )

    except Exception as e:
        logger.error("Signal 1 check encountered error for Job %s: %s", clean_id, e)

    # ─────────────────────────────────────────────────────────────
    # SIGNAL 2: LinkedIn Applied Jobs Tracker View
    # ─────────────────────────────────────────────────────────────
    try:
        tracker_url = "https://www.linkedin.com/my-items/saved-jobs/?cardType=APPLIED"
        record_provider_read(
            operation_type=ProviderOperationType.JOB_DETAIL,
            resource_identifier="applied_tracker",
            cache_hit=False,
            linkedin_profile=prof,
            workspace=ws,
            success=True,
            read_cost=1,
        )
        page.goto(tracker_url, wait_until="commit", timeout=35000)
        page.wait_for_timeout(3500)

        # Look for job links matching clean_id in the applied list
        applied_links = page.locator(f'a[href*="/jobs/view/{clean_id}"], a[href*="currentJobId={clean_id}"]').all()
        if len(applied_links) > 0:
            logger.info("Signal 2 verified: Job ID %s present in LinkedIn Applied Jobs tracker", clean_id)
            return VerificationResult(
                True,
                ConfirmationEvidenceType.LINKEDIN_TRACKER,
                {
                    "external_job_id": clean_id,
                    "verification_method": "signal_2_applied_tracker",
                    "tracker_url": tracker_url,
                    "verified_at": timezone.now().isoformat()
                },
                status=VerificationStatus.CONFIRMED_APPLIED
            )

    except Exception as e:
        logger.error("Signal 2 check encountered error for Job %s: %s", clean_id, e)

    # If neither top card nor tracker showed positive applied badge, but no active apply button was found:
    # This is UNKNOWN (selector changed / tracker indexing delay), NOT definitive NOT_APPLIED!
    logger.info("Job-specific verification for Job %s: UNKNOWN (unverified)", clean_id)
    return VerificationResult(
        False,
        "tracker_unmatched_or_unavailable",
        {"external_job_id": clean_id, "reason": "No positive badge or active button found; tracker unverified"},
        status=VerificationStatus.UNKNOWN
    )


class LinkedInEasyApplyAdapter:
    """Production Playwright adapter for LinkedIn Easy Apply applications."""

    def __init__(self, session=None):
        self.session = session

    def apply(
        self,
        job_url: str,
        answers: Optional[Dict[str, Any]] = None,
        candidate=None,
        draft=None,
        external_action_authorized: bool = True
    ) -> Dict[str, Any]:
        """Execute real LinkedIn Easy Apply flow in browser."""
        if not self.session:
            from openoutreach.linkedin.browser.registry import get_first_active_profile, get_or_create_session
            profile = get_first_active_profile()
            if not profile:
                return {"success": False, "error": "no_active_linkedin_profile"}
            self.session = get_or_create_session(profile)

        from openoutreach.linkedin.services.account_governor import (
            check_and_record_safety_warning,
            is_account_safe_for_provider_read,
        )
        from openoutreach.linkedin.services.evidence_cache import record_provider_read
        from openoutreach.linkedin.models import ProviderOperationType

        ws = getattr(candidate, "workspace", None)
        if not ws and hasattr(candidate, "user") and candidate.user:
            from openoutreach.core.models import WorkspaceMembership
            mem = WorkspaceMembership.objects.filter(user=candidate.user, status="active").first()
            if mem:
                ws = mem.workspace
        if not ws and draft:
            ws = getattr(draft, "workspace", None)
        prof = getattr(self.session, "linkedin_profile", None)

        is_safe, safety_reason = is_account_safe_for_provider_read(
            workspace=ws,
            linkedin_profile=prof,
        )
        if not is_safe:
            logger.warning("Easy Apply blocked by governor: %s", safety_reason)
            return {"success": False, "error": f"governor_blocked: {safety_reason}"}

        self.session.ensure_browser()
        page = self.session.page

        if not job_url:
            return {"success": False, "error": "missing_job_url"}

        ext_job_id = extract_linkedin_job_id(job_url)
        logger.info("Opening job URL for Easy Apply: %s (External ID: %s)", job_url, ext_job_id)

        try:
            # Navigate to job search view with currentJobId and Easy Apply filter
            target_url = f"https://www.linkedin.com/jobs/search/?currentJobId={ext_job_id}&f_AL=true" if ext_job_id else job_url
            page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            check_and_record_safety_warning(page, workspace=ws, linkedin_profile=prof)
            page.wait_for_timeout(4000)
            record_provider_read(
                operation_type=ProviderOperationType.EASY_APPLY,
                resource_identifier=str(ext_job_id or job_url),
                cache_hit=False,
                linkedin_profile=prof,
                workspace=ws,
                success=True,
                read_cost=2,
            )
        except Exception as e:
            logger.error("Navigation failed to %s: %s", job_url, e)
            record_provider_read(
                operation_type=ProviderOperationType.EASY_APPLY,
                resource_identifier=str(ext_job_id or job_url),
                cache_hit=False,
                linkedin_profile=prof,
                workspace=ws,
                success=False,
                read_cost=2,
            )
            return {"success": False, "error": f"navigation_failed: {str(e)}"}

        # 1. Check if already applied using scoped job-specific check
        is_app, ev_type, ev_meta = verify_linkedin_job_applied(self.session, ext_job_id or "", job_url=job_url, page=page)
        if is_app:
            logger.info("Job already shows Applied on LinkedIn: %s (ID: %s)", job_url, ext_job_id)
            return {
                "success": True,
                "externally_verified": True,
                "confirmation_type": ev_type,
                "evidence": "Applied",
                "metadata": ev_meta,
                "reason": "already_applied_on_linkedin"
            }

        # 2. Ensure page is on target job URL and detect Easy Apply button in top card
        if f"currentJobId={ext_job_id}" not in page.url and (ext_job_id and ext_job_id not in page.url):
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.error("Navigation back to target_url failed: %s", e)

        apply_btn = None
        for _ in range(10):
            candidate_btns = page.locator(
                '.jobs-details__main-content button.jobs-apply-button, '
                '.job-details-jobs-unified-top-card button.jobs-apply-button, '
                '.jobs-search__job-details button.jobs-apply-button, '
                '.jobs-s-apply button, '
                'button.jobs-apply-button, '
                'button.apply-button, '
                '.top-card-layout button, '
                'button.top-card-layout__cta, '
                'button:has-text("Easy Apply"), '
                'button[aria-label*="Easy Apply"], '
                'button[aria-label*="Apply to "]'
            ).all()
            apply_btn = next((b for b in candidate_btns if b.is_visible() and "search-reusables" not in (b.get_attribute("class") or "") and "filter" not in (b.get_attribute("aria-label") or "").lower()), None)
            if apply_btn:
                break
            page.wait_for_timeout(1000)

        if not apply_btn:
            return {
                "success": False,
                "error": "easy_apply_not_found",
                "reason": "Job does not have an active LinkedIn Easy Apply button."
            }

        aria_label = (apply_btn.get_attribute("aria-label") or "").lower()
        btn_text = apply_btn.inner_text().strip().lower()
        btn_class = (apply_btn.get_attribute("class") or "").lower()

        if "company website" in aria_label or "on company website" in aria_label or "on company website" in btn_text:
            return {
                "success": False,
                "error": "not_easy_apply",
                "reason": "Job requires external company website application."
            }

        is_easy_apply = (
            "linkedin apply" in aria_label
            or "apply to" in aria_label
            or "easy apply" in aria_label
            or "easy apply" in btn_text
            or "jobs-apply-button" in btn_class
            or "apply-button" in btn_class
            or btn_text == "apply"
            or apply_btn.locator('svg[data-test-icon="linkedin-bug-xxsmall"]').count() > 0
        )

        if not is_easy_apply:
            return {
                "success": False,
                "error": "not_easy_apply",
                "reason": "Job requires external company website application."
            }

        # 3. Click Easy Apply button to open modal
        logger.info("Clicking Easy Apply button: %s (%s)", btn_text, aria_label)
        try:
            apply_btn.click()
            page.wait_for_timeout(3000)
        except Exception as e:
            return {"success": False, "error": f"click_apply_failed: {str(e)}"}

        modal = None
        for _ in range(8):
            cand_modals = page.locator('.jobs-easy-apply-modal, div[data-test-modal], div[role="dialog"], .artdeco-modal').all()
            modal = next((m for m in cand_modals if m.is_visible()), None)
            if modal:
                break
            page.wait_for_timeout(1000)

        if not modal:
            return {"success": False, "error": "modal_not_opened"}

        # 4. Step through modal wizard with factual candidate data
        step = 0
        max_steps = 10
        unknown_required_count = 0

        while step < max_steps:
            step += 1
            page.wait_for_timeout(1500)

            if modal.count() == 0 or not modal.is_visible():
                break

            # 4a. Handle Resume Upload / Selection (Strictly use job-specific tailored artifact)
            file_inputs = modal.locator('input[type="file"]').all()
            uploaded_resume_hash = None
            uploaded_resume_path = None
            for file_inp in file_inputs:
                try:
                    s_cv = getattr(draft, "structured_cv", {}) if draft else {}
                    resume_path = s_cv.get("resume_docx_path") or s_cv.get("resume_pdf_path")
                    expected_hash = s_cv.get("resume_hash")
                    if resume_path and os.path.exists(resume_path):
                        logger.info("Uploading job-specific tailored resume artifact: %s", resume_path)
                        file_inp.set_input_files(resume_path)
                        uploaded_resume_path = resume_path
                        uploaded_resume_hash = expected_hash
                        page.wait_for_timeout(2000)
                except Exception as e:
                    logger.warning("Resume file upload note: %s", e)

            # 4b. Handle Cover Letter Textarea
            cl_text = getattr(draft, "cover_letter", "") if draft else ""
            if cl_text:
                cl_areas = modal.locator('textarea[id*="cover"], textarea[name*="cover"], textarea[aria-label*="Cover"], textarea[aria-label*="cover"]').all()
                for cl_area in cl_areas:
                    try:
                        if not cl_area.input_value():
                            logger.info("Filling job-specific tailored cover letter into textarea.")
                            cl_area.fill(cl_text)
                    except Exception as e:
                        logger.debug("Cover letter area note: %s", e)

            from openoutreach.career.answer_bank import get_answer_for_question

            # 4c. Handle radio button fieldsets strictly from candidate qualifications
            fieldsets = modal.locator('fieldset').all()
            for fs in fieldsets:
                try:
                    legend = fs.locator('legend').first
                    leg_text = legend.inner_text().strip() if legend and legend.count() > 0 else ""
                    radios = fs.locator('input[type="radio"]').all()
                    has_checked = any(r.is_checked() for r in radios)
                    if not has_checked and leg_text:
                        ans_status, ans_val, c_key = get_answer_for_question(candidate, leg_text, job=job)
                        if ans_status == "ANSWER_FOUND":
                            str_val = "Yes" if ans_val is True else ("No" if ans_val is False else str(ans_val))
                            target_radio = fs.locator(f'label:has-text("{str_val}"), input[type="radio"][value="{str_val}"]').first
                            if target_radio.count() > 0:
                                target_radio.click()
                        elif ans_status == "OPTIONAL_SKIP":
                            decline_opt = fs.locator('label:has-text("Decline"), label:has-text("Prefer not")').first
                            if decline_opt.count() > 0:
                                decline_opt.click()
                        else:
                            unknown_required_count += 1
                            unknown_questions.append({
                                "question": leg_text,
                                "type": "radio",
                                "canonical_key": c_key,
                                "options": [r.get_attribute("value") for r in radios]
                            })
                except Exception as e:
                    logger.debug("Fieldset processing note: %s", e)

            # 4d. Handle dropdown selects strictly from candidate qualifications
            selects = modal.locator('select').all()
            for sel in selects:
                try:
                    sel_val = sel.input_value() if hasattr(sel, "input_value") else ""
                    if not sel_val or sel_val in ["Select an option", ""]:
                        sel_id = sel.get_attribute("id")
                        lbl_el = modal.locator(f'label[for="{sel_id}"]').first if sel_id else None
                        lbl_text = lbl_el.inner_text().strip() if lbl_el and lbl_el.count() > 0 else ""
                        if lbl_text:
                            ans_status, ans_val, c_key = get_answer_for_question(candidate, lbl_text, job=job)
                            if ans_status == "ANSWER_FOUND":
                                try:
                                    sel.select_option(label=str(ans_val))
                                except Exception:
                                    try:
                                        sel.select_option(value=str(ans_val))
                                    except Exception:
                                        pass
                            elif "country" in lbl_text.lower() or "code" in lbl_text.lower():
                                phone_val = getattr(candidate, "phone", "") or ""
                                if "+92" in phone_val or "Pakistan" in getattr(candidate, "location", ""):
                                    try:
                                        sel.select_option(label="Pakistan (+92)")
                                    except Exception:
                                        pass
                            else:
                                unknown_required_count += 1
                                unknown_questions.append({
                                    "question": lbl_text,
                                    "type": "select",
                                    "canonical_key": c_key
                                })
                except Exception as e:
                    logger.debug("Select processing note: %s", e)

            # 4e. Handle text, number, and textarea inputs strictly from candidate facts
            inputs = modal.locator('input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]), textarea').all()
            for inp in inputs:
                try:
                    inp_type = (inp.get_attribute("type") or "text").lower()
                    inp_val = (inp.input_value() if hasattr(inp, "input_value") else "") or ""
                    inp_val_clean = inp_val.strip()

                    inp_id = inp.get_attribute("id")
                    label_el = modal.locator(f'label[for="{inp_id}"]').first if inp_id else None
                    lbl_text = label_el.inner_text().strip() if label_el and label_el.count() > 0 else ""
                    if not lbl_text:
                        parent_grp = inp.locator('xpath=ancestor::div[contains(@class, "jobs-easy-apply-form-section__grouping") or contains(@class, "fb-dash-form-element") or contains(@class, "artdeco-text-input")]').first
                        if parent_grp and parent_grp.count() > 0:
                            grp_lbl = parent_grp.locator('span[aria-hidden="true"], label, legend, .fb-dash-form-element__label, .artdeco-text-input--label').first
                            if grp_lbl and grp_lbl.count() > 0:
                                lbl_text = grp_lbl.inner_text().strip()
                    if not lbl_text:
                        lbl_text = (inp.get_attribute("aria-label") or inp.get_attribute("placeholder") or "").strip()
                    
                    clean_lbl = re.sub(r'(\bRequired\b|\*|[\r\n\t]+)', ' ', lbl_text).strip()
                    clean_lbl = re.sub(r'\s+', ' ', clean_lbl)
                    lbl_lower = clean_lbl.lower()

                    if "phone" in lbl_lower or "mobile" in lbl_lower:
                        phone = getattr(candidate, "phone", "")
                        if phone:
                            if not inp_val_clean:
                                inp.fill(phone)
                        else:
                            unknown_required_count += 1
                            unknown_questions.append({"question": "Phone Number", "type": "text", "canonical_key": "phone"})
                    elif "email" in lbl_lower:
                        email = getattr(candidate, "email", "")
                        if email and not inp_val_clean:
                            inp.fill(email)
                    elif "first name" in lbl_lower:
                        first = candidate.full_name.split()[0] if candidate and candidate.full_name else ""
                        if first and not inp_val_clean:
                            inp.fill(first)
                    elif "last name" in lbl_lower:
                        last = candidate.full_name.split()[-1] if candidate and candidate.full_name else ""
                        if last and not inp_val_clean:
                            inp.fill(last)
                    elif "city" in lbl_lower or "location" in lbl_lower:
                        loc = getattr(candidate, "location", "")
                        if loc:
                            if not inp_val_clean:
                                inp.fill(loc)
                        else:
                            unknown_required_count += 1
                            unknown_questions.append({"question": "Location / City", "type": "text", "canonical_key": "location"})
                    elif clean_lbl:
                        ans_status, ans_val, c_key = get_answer_for_question(candidate, clean_lbl, job=job)
                        if ans_status == "ANSWER_FOUND":
                            val_str = str(ans_val).strip()
                            # Clean integer questions
                            if inp_type == "number" or "years" in (c_key or "") or inp.get_attribute("inputmode") == "numeric":
                                num_match = re.search(r'\d+', val_str)
                                if num_match:
                                    val_str = num_match.group(0)
                            if inp_val_clean != val_str:
                                try:
                                    inp.click()
                                    inp.fill("")
                                    inp.fill(val_str)
                                    inp.dispatch_event("input", {"bubbles": True, "cancelable": True})
                                    inp.dispatch_event("change", {"bubbles": True, "cancelable": True})
                                    inp.dispatch_event("blur", {"bubbles": True, "cancelable": True})
                                except Exception as fill_err:
                                    logger.debug("Input fill note: %s", fill_err)
                        elif not inp_val_clean or inp_val_clean in ["0", "None"]:
                            unknown_required_count += 1
                            unknown_questions.append({
                                "question": clean_lbl,
                                "type": "text",
                                "canonical_key": c_key
                            })
                except Exception as e:
                    logger.debug("Input processing note: %s", e)

            if unknown_required_count > 0:
                logger.warning("Encountered unknown required screening question(s). Stopping modal safely.")
                self._dismiss_modal(page, modal)
                return {
                    "success": False,
                    "status": "needs_your_answer",
                    "error": "unknown_required_answers",
                    "reason": "Encountered unknown required screening questions needing operator input.",
                    "unknown_questions": unknown_questions,
                }

            # Check for Submit application button
            submit_btn = modal.locator('button[aria-label="Submit application"], button:has-text("Submit application")').first
            if submit_btn.count() > 0 and submit_btn.is_visible():
                logger.info("Reached Submit application step on LinkedIn.")
                if not external_action_authorized:
                    self._dismiss_modal(page, modal)
                    return {
                        "success": False,
                        "error": "external_authorization_required",
                        "reason": "Application reviewed and ready for operator submit authorization."
                    }

                # Durable ProviderWriteIntent Claim
                from openoutreach.linkedin.models import ProviderWriteIntent
                from openoutreach.linkedin.services.idempotency import (
                    claim_provider_write,
                    record_provider_write_attempt,
                    confirm_provider_write,
                    fail_provider_write,
                )
                profile = getattr(self.session, "linkedin_profile", None)
                ws = getattr(profile, "workspace", None) if profile else None
                allowed, write_reason, write_intent = claim_provider_write(
                    workspace=ws,
                    linkedin_profile=profile,
                    operation_type=ProviderWriteIntent.OperationType.EASY_APPLY_SUBMIT,
                    target_identity=str(ext_job_id or job_url),
                    logical_action_identity=f"job_{getattr(candidate, 'id', 'cand')}_{ext_job_id}",
                )
                if not allowed:
                    logger.warning("Easy Apply submission for Job %s blocked by write intent: %s", ext_job_id, write_reason)
                    self._dismiss_modal(page, modal)
                    return {
                        "success": False,
                        "error": "write_intent_blocked",
                        "reason": f"Durable idempotency blocked submission: {write_reason}",
                    }

                # Submit the real application (maximum 1 click)
                logger.info("Submitting real LinkedIn application for Job ID %s...", ext_job_id)
                record_provider_write_attempt(write_intent)
                try:
                    submit_btn.click()
                except Exception as click_err:
                    fail_provider_write(write_intent, str(click_err), before_provider=False)
                    raise click_err

                page.wait_for_timeout(4000)

                # Capture post-submit confirmation dialog state BEFORE clicking Done/Dismiss
                post_submit_confirmed = False
                post_meta = {
                    "provider": "linkedin",
                    "evidence_type": ConfirmationEvidenceType.POST_SUBMIT_CONFIRMATION,
                    "external_job_id": ext_job_id,
                    "company": getattr(candidate, "company", "") or (job.company if 'job' in locals() and job else ""),
                    "job_title": getattr(job, "title", "") if 'job' in locals() and job else "",
                    "confirmed_at": timezone.now().isoformat(),
                    "method": "post_submit_modal_confirmation"
                }

                # Check post-submit confirmation indicators in the active modal / page
                confirm_heading = page.locator(
                    'h2:has-text("Application submitted"), '
                    'h2:has-text("Your application was submitted"), '
                    'h3:has-text("Application submitted"), '
                    'div:has-text("Application submitted"), '
                    '.artdeco-modal__header:has-text("Application submitted"), '
                    '.jobs-post-apply-header'
                ).first
                if confirm_heading.count() > 0 and confirm_heading.is_visible():
                    logger.info("Signal A verified: Post-submit confirmation dialog captured for Job %s", ext_job_id)
                    post_submit_confirmed = True
                    post_meta["dialog_header"] = confirm_heading.inner_text().strip()

                # Dismiss confirmation dialog if present
                done_btn = page.locator('button[aria-label="Dismiss"], button:has-text("Done"), button:has-text("Close")').first
                if done_btn.count() > 0 and done_btn.is_visible():
                    done_btn.click()
                    page.wait_for_timeout(2000)

                if post_submit_confirmed:
                    confirm_provider_write(write_intent, confirmation_data=post_meta)
                    return {
                        "success": True,
                        "externally_verified": True,
                        "confirmation_type": ConfirmationEvidenceType.POST_SUBMIT_CONFIRMATION,
                        "evidence": "Application submitted dialog",
                        "metadata": post_meta,
                        "submitted_at": timezone.now().isoformat()
                    }

                # Deterministic post-submit verification via secondary signals
                verif_res = verify_linkedin_job_applied(
                    self.session, ext_job_id or "", job_url=job_url, page=page
                )

                if verif_res.is_applied:
                    confirm_provider_write(write_intent, confirmation_data=verif_res.metadata)
                    return {
                        "success": True,
                        "externally_verified": True,
                        "confirmation_type": verif_res.evidence_type,
                        "evidence": "Applied",
                        "metadata": verif_res.metadata,
                        "submitted_at": timezone.now().isoformat()
                    }
                else:
                    fail_provider_write(write_intent, "unverified_post_submit", before_provider=False)
                    return {
                        "success": True,
                        "externally_verified": False,
                        "confirmation_type": "submission_unverified",
                        "evidence": "Modal submitted without confirmed dialog",
                        "metadata": {"external_job_id": ext_job_id, "reason": "unverified_post_submit", "verification_status": verif_res.status},
                        "submitted_at": timezone.now().isoformat()
                    }

            # Check for Review button
            review_btn = modal.locator('button[aria-label*="Review"], button:has-text("Review")').first
            if review_btn.count() > 0 and review_btn.is_visible():
                logger.info("Clicking Review button in modal...")
                review_btn.click()
                page.wait_for_timeout(2500)
                continue

            # Check for Next button
            next_btn = modal.locator('button[aria-label*="next"], button:has-text("Next")').first
            if next_btn.count() > 0 and next_btn.is_visible():
                logger.info("Clicking Next button in modal...")
                next_btn.click()
                page.wait_for_timeout(2500)
                continue

            break

        # Check for unhandled questions or error feedback in the modal before dismissing
        error_items = modal.locator('.artdeco-inline-feedback--error, [data-test-form-builder-error]').all()
        extracted_unhandled = []
        for err in error_items:
            try:
                parent_section = err.locator('xpath=ancestor::div[contains(@class, "jobs-easy-apply-form-section__grouping")]').first
                label = parent_section.locator('label, legend').first if parent_section.count() > 0 else modal.locator('label, legend').first
                q_text = label.inner_text().strip() if label and label.count() > 0 else ""
                err_text = err.inner_text().strip()
                if q_text:
                    from openoutreach.career.answer_bank import normalize_question
                    extracted_unhandled.append({
                        "question": q_text,
                        "error_text": err_text,
                        "canonical_key": normalize_question(q_text) or q_text.lower().replace(" ", "_")[:50]
                    })
            except Exception:
                pass

        self._dismiss_modal(page, modal)
        if extracted_unhandled:
            return {
                "success": False,
                "status": "needs_your_answer",
                "error": "unknown_required_answers",
                "reason": "Encountered required form fields with validation errors needing operator answer.",
                "unknown_questions": extracted_unhandled,
            }

        return {
            "success": False,
            "error": "application_flow_incomplete",
            "reason": "Could not reach Submit step within maximum wizard steps."
        }

    def _dismiss_modal(self, page, modal):
        """Safely dismiss Easy Apply modal without saving draft."""
        try:
            close_btn = modal.locator('button[aria-label="Dismiss"], button[data-test-modal-close-btn]').first
            if close_btn.count() > 0:
                close_btn.click()
                page.wait_for_timeout(1000)
                discard_btn = page.locator('button[data-control-name="discard_application_confirm_btn"], button:has-text("Discard")').first
                if discard_btn.count() > 0:
                    discard_btn.click()
                    page.wait_for_timeout(1000)
        except Exception as e:
            logger.debug("Modal dismissal note: %s", e)
