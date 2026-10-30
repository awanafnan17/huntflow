"""Diagnose profile page heading elements in LinkedIn's alternate layout."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openoutreach.settings")
import django
django.setup()

from scripts.verify_real_external_account import resolve_linkedin_session_credentials

def run():
    session_res = resolve_linkedin_session_credentials(profile_id=1)
    from openoutreach.linkedin.browser.adapter import launch_provider_browser as launch_browser
    page, context, browser, playwright = launch_browser(storage_state=session_res["cookie_data"], headed=True)

    try:
        page.goto("https://www.linkedin.com/in/jango-mango-235118423/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(5000)

        print("=== PROFILE PAGE HEADING DIAGNOSIS ===\n")
        print(f"URL: {page.url}")
        print(f"Title: {page.title()}")

        # All h1 elements
        h1s = page.locator("h1")
        print(f"\nAll <h1> elements: {h1s.count()}")
        for i in range(min(h1s.count(), 10)):
            h1 = h1s.nth(i)
            try:
                info = h1.evaluate("""el => ({
                    text: el.textContent.trim().substring(0, 100),
                    className: (el.className || '').substring(0, 80),
                    visible: el.offsetParent !== null,
                    inMain: el.closest('main') !== null,
                    inSection: el.closest('section') !== null,
                    bbox: el.getBoundingClientRect(),
                })""")
                vis = "✓" if info['visible'] else "✗"
                print(f"  [{i}] {vis} text='{info['text']}' class='{info['className']}' inMain={info['inMain']} inSection={info['inSection']} pos=({int(info['bbox']['x'])},{int(info['bbox']['y'])})")
            except Exception as e:
                print(f"  [{i}]: error {e}")

        # All h2 elements in main
        h2s = page.locator("main h2")
        print(f"\nAll <h2> in main: {h2s.count()}")
        for i in range(min(h2s.count(), 10)):
            h2 = h2s.nth(i)
            try:
                text = (h2.text_content() or "").strip()[:80]
                vis = h2.is_visible()
                print(f"  [{i}] {'✓' if vis else '✗'} text='{text}'")
            except:
                pass

        # All span.t-bold or span.text-heading in main
        print(f"\n=== BOLD/HEADING SPANS IN MAIN ===")
        for sel in ["main span.t-bold", "main [class*='heading']", "main [class*='title']"]:
            els = page.locator(sel)
            count = els.count()
            if count > 0:
                print(f"\n  '{sel}' count: {count}")
                for i in range(min(count, 5)):
                    try:
                        text = (els.nth(i).text_content() or "").strip()[:80]
                        vis = els.nth(i).is_visible()
                        print(f"    [{i}] {'✓' if vis else '✗'} text='{text}'")
                    except:
                        pass

        # Look for the name by searching for elements near the profile photo
        print(f"\n=== PROFILE PHOTO CONTEXT ===")
        profile_figs = page.locator("main figure:has(img[src*='profile-displayphoto'])")
        print(f"Profile photo figures in main: {profile_figs.count()}")
        for i in range(min(profile_figs.count(), 3)):
            fig = profile_figs.nth(i)
            try:
                info = fig.evaluate("""el => {
                    // Search siblings and nearby elements for name text
                    let parent = el.parentElement;
                    let results = [];
                    for (let lvl = 0; lvl < 5 && parent; lvl++) {
                        // Check all text-containing children
                        let spans = parent.querySelectorAll('span, h1, h2, h3, p, div');
                        for (let s of spans) {
                            let text = s.textContent.trim();
                            if (text.length > 1 && text.length < 60 && s.children.length === 0) {
                                let rect = s.getBoundingClientRect();
                                if (rect.width > 0 && rect.height > 0) {
                                    results.push({
                                        tag: s.tagName,
                                        text: text.substring(0, 60),
                                        className: (s.className || '').substring(0, 50),
                                        y: Math.round(rect.y),
                                        fontSize: window.getComputedStyle(s).fontSize,
                                        fontWeight: window.getComputedStyle(s).fontWeight,
                                    });
                                }
                            }
                        }
                        parent = parent.parentElement;
                    }
                    // Sort by y position to find topmost (likely name)
                    results.sort((a, b) => a.y - b.y);
                    return results.slice(0, 15);
                }""")
                print(f"\n  Figure [{i}] nearby text elements:")
                for j, r in enumerate(info):
                    print(f"    [{j}] <{r['tag']}> y={r['y']} size={r['fontSize']} weight={r['fontWeight']} class='{r['className']}' text='{r['text']}'")
            except Exception as e:
                print(f"  Figure [{i}]: error {e}")

        # sections in main
        sections = page.locator("main section")
        print(f"\n=== SECTIONS IN MAIN: {sections.count()} ===")
        for i in range(min(sections.count(), 5)):
            sec = sections.nth(i)
            try:
                info = sec.evaluate("""el => ({
                    id: el.id || '',
                    className: (el.className || '').substring(0, 60),
                    hasH1: el.querySelector('h1') !== null,
                    hasH2: el.querySelector('h2') !== null,
                    hasProfileImg: el.querySelector('figure img[src*=\"profile-displayphoto\"]') !== null,
                    firstText: el.textContent.trim().substring(0, 100),
                })""")
                print(f"  [{i}] id='{info['id']}' h1={info['hasH1']} h2={info['hasH2']} profileImg={info['hasProfileImg']}")
                print(f"       text: {info['firstText']}")
            except:
                pass

        print("\n\nDone. Press ENTER to close.")
        input()

    finally:
        try:
            context.close(); browser.close(); playwright.stop()
        except: pass

if __name__ == "__main__":
    run()
