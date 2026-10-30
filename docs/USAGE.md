# Using HuntFlow

## 1. Start locally

Follow the repository quick start, create your own administrator, and open `/dashboard/`. A new checkout has no real accounts or candidate records. The web server lets you inspect and configure the application; the daemon executes queued automation separately.

## 2. Create the workspace and sender

Use onboarding to create or select your workspace, define the purpose of your campaign, and add a sender identity. Keep candidate identity, LinkedIn account identity, and campaign sender consistent. Review workspace membership and roles before inviting collaborators.

The dashboard contains campaigns, leads, drafts, LinkedIn accounts, email configuration, tasks, operational status, career profiles and jobs. Some operations are also exposed through Django Admin for development and inspection.

## 3. Configure the language model

Choose a provider and model in the LLM settings and provide your own API key if that provider needs one. You can use Ollama for local generation. A model setting such as `ollama:qwen2.5:1.5b` routes to Ollama; the exact installed model name must match. On a host machine, set `OLLAMA_BASE_URL=http://127.0.0.1:11434` explicitly. The Docker deployment expects its internal `ollama` service name.

Model verification makes a real inference request. Check prompts and uploaded candidate information before sending them to a hosted provider.

## 4. Configure LinkedIn outreach

1. Add your own LinkedIn account in the integration/account UI and complete the interactive verification flow.
2. Check the account's session and identity status. Stop if it shows a challenge, safety stop, or an identity mismatch.
3. Create a campaign with a clear purpose, target roles/companies/locations and factual sender context.
4. Review the connection-note template and follow-up sequence. Use only supported claims about the recipient and sender.
5. Set conservative workspace and campaign action limits, review drafts, and enable only the actions you intend to run.
6. Start `python manage.py rundaemon` in a separate terminal when ready.
7. Inspect tasks, logs and conversation state. Distinguish a queued attempt from a confirmed connection/message outcome.

The implementation includes connection requests, messages and follow-ups. Browser automation is sensitive to provider changes. A retry must not ignore account safety stops or create duplicate sends.

## 5. Add email outreach, if needed

Configure a mailbox and its SMTP settings using your own credentials. A work-email finder integration is optional. The outreach pipeline can route qualified leads to email when it has an address, otherwise retain the LinkedIn route. Email and LinkedIn outcomes are tracked separately. Verify addresses, suppression state, campaign scope and the message before enabling sends.

## 6. Prepare candidate information

1. Open the career workspace and create your candidate profile.
2. Upload your own PDF/DOCX resume or paste source text.
3. Run extraction and review the proposed facts. Correct missing or inaccurate details.
4. Review and approve the candidate profile version; keep its supporting document lineage.
5. Complete application profile/answer-bank settings, including location preferences and questions requiring explicit answers.

The public release removes archived personal CV fallbacks. If AI extraction or tailoring fails, it raises an error rather than returning somebody else's identity or qualifications. Review legacy identity-recovery commands before use: several remain specialized historical development utilities.

## 7. Discover and apply to jobs

1. Set role, location and other search preferences.
2. Discover jobs or add a supported job URL and inspect the verified provider identity and description.
3. Review matching evidence and any missing requirements. Match/ATS scores are heuristics, not hiring predictions.
4. Generate a draft resume/cover letter using the approved candidate facts, and review the generated documents.
5. Approve the relevant package/action and enable the intended workspace application controls.
6. The supported Easy Apply path can answer known fields, upload the prepared package and submit the application when its gates pass.
7. Resolve unknown questions in the screening inbox instead of inventing answers. Verify the recorded submission evidence and reconcile uncertain outcomes before retrying.

External application sites and arbitrary forms are not guaranteed to work. A provider page can change; a task marked attempted is not necessarily a successful application.

## 8. Stop, review and resume

Use the dashboard's automation controls and safety states to pause activity. Stop the daemon process when performing local maintenance. Review account/session health, pending tasks, authorization state and confirmed external outcomes before resuming. Never treat a synthetic demonstration as real provider evidence.

## Troubleshooting

| Symptom | Check |
|---|---|
| Database cannot open | Create `data/`, then run migrations from the repository root |
| Login fails | Use the administrator you created; there is no shared demo password |
| Browser executable missing | Run `python -m playwright install chromium` in the active environment |
| Local Ollama cannot connect | Set the explicit host endpoint; Docker service names do not resolve on the host |
| LLM extraction fails | Verify the selected model and credentials, inspect source text, and retry without bypassing fact review |
| Tasks remain blocked | Inspect campaign authorization, profile readiness, account identity, quotas and safety state |
| Submission is uncertain | Review provider-specific confirmation evidence; do not assume success or blindly resubmit |
