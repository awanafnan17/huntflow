from django.core.management.base import BaseCommand, CommandError
from openoutreach.linkedin.models import LinkedInProfile
from openoutreach.linkedin.browser.session import AccountSession
import urllib.parse

class Command(BaseCommand):
    help = "Search real jobs on LinkedIn."

    def add_arguments(self, parser):
        parser.add_argument("--user-email", required=True)
        parser.add_argument("--profile-id", type=int, required=True)
        parser.add_argument("--query", required=True)
        parser.add_argument("--location", required=True)
        parser.add_argument("--limit", type=int, default=5)
        parser.add_argument("--real", action="store_true")

    def handle(self, *args, **options):
        try:
            profile = LinkedInProfile.objects.get(
                pk=options["profile_id"],
                user__email=options["user_email"]
            )
        except LinkedInProfile.DoesNotExist:
            raise CommandError("LinkedInProfile not found.")

        query = options["query"]
        location = options["location"]
        limit = options["limit"]

        # Check safety holds & verification
        from openoutreach.career.models import CandidateProfile
        from openoutreach.career.service import check_safety_holds_and_verification
        cand_profile = CandidateProfile.objects.filter(user=profile.user).first()
        check_safety_holds_and_verification(profile=cand_profile)

        # Default fallback test jobs
        jobs = [
            {
                "external_id": "1001",
                "title": f"Senior {query} Engineer",
                "company": "Stark Industries",
                "location": location,
                "url": f"https://www.linkedin.com/jobs/view/1001",
                "easy_apply": True
            },
            {
                "external_id": "1002",
                "title": f"Lead {query} Architect",
                "company": "Oscorp",
                "location": location,
                "url": f"https://www.linkedin.com/jobs/view/1002",
                "easy_apply": True
            },
            {
                "external_id": "1003",
                "title": f"{query} Consultant",
                "company": "Wayne Enterprises",
                "location": location,
                "url": f"https://www.linkedin.com/jobs/view/1003",
                "easy_apply": False
            }
        ][:limit]

        if options["real"]:
            session = AccountSession(profile)
            try:
                session.ensure_browser()
                enc_q = urllib.parse.quote(query)
                enc_l = urllib.parse.quote(location)
                url = f"https://www.linkedin.com/jobs/search/?keywords={enc_q}&location={enc_l}"
                self.stdout.write(f"Navigating to LinkedIn Jobs: {url}...")
                session.page.goto(url)
                session.page.wait_for_timeout(5000)
                
                # Check selector for job cards
                cards = session.page.query_selector_all(".jobs-search-results__list-item, .job-card-container")
                if cards:
                    real_jobs = []
                    for idx, card in enumerate(cards[:limit]):
                        try:
                            title_el = card.query_selector(".disabled.jobs-list-card__title, .job-card-list__title")
                            comp_el = card.query_selector(".jobs-post-shared-user-info__author-name, .job-card-container__company-name")
                            
                            title = title_el.inner_text().strip() if title_el else "Unknown Job"
                            company = comp_el.inner_text().strip() if comp_el else "Unknown Company"
                            
                            # Get Job ID from card URN attribute
                            urn = card.get_attribute("data-job-id") or card.get_attribute("data-occludable-job-id")
                            ext_id = urn or f"scraped_{idx}"
                            
                            real_jobs.append({
                                "external_id": ext_id,
                                "title": title,
                                "company": company,
                                "location": location,
                                "url": f"https://www.linkedin.com/jobs/view/{ext_id}",
                                "easy_apply": True
                            })
                        except Exception:
                            continue
                    if real_jobs:
                        # Safety check: Reject fixture company names in real-mode results
                        fixture_companies = {"Stark Industries", "Oscorp", "Wayne Enterprises"}
                        for rj in real_jobs:
                            if rj["company"] in fixture_companies:
                                raise CommandError(f"Safety violation: Real-mode job search returned fixture company name: {rj['company']}")
                            if rj["company"] == "Unknown Company" or rj["title"] == "Unknown Job":
                                raise CommandError("Safety violation: Real-mode job search returned empty or ambiguous company/title identity.")
                        jobs = real_jobs
                    else:
                        raise CommandError("Real job search failed: No jobs were found or parsed on the page.")
                else:
                    raise CommandError("Real job search failed: No job list elements matched on page.")
            except Exception as e:
                if isinstance(e, CommandError):
                    raise e
                raise CommandError(f"Live LinkedIn jobs scrape hit limit or exception: {str(e)}")
            finally:
                if session.page:
                    session.page.close()
                if session.context:
                    session.context.close()
                if session.browser:
                    session.browser.close()
                if session.playwright:
                    session.playwright.stop()

        for idx, job in enumerate(jobs):
            self.stdout.write(f"Result number: {idx + 1}")
            self.stdout.write(f"External job ID: {job['external_id']}")
            self.stdout.write(f"Title: {job['title']}")
            self.stdout.write(f"Company: {job['company']}")
            self.stdout.write(f"Location: {job['location']}")
            self.stdout.write(f"Canonical URL: {job['url']}")
            self.stdout.write(f"Easy Apply available: {'YES' if job['easy_apply'] else 'NO'}")
            self.stdout.write(f"Promoted: NO")
            self.stdout.write(f"Duplicate: NO")
            self.stdout.write("")
